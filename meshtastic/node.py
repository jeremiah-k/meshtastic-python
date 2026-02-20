"""Node class."""

# pylint: disable=too-many-lines

import base64
import binascii
import logging
import threading
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    NoReturn,
    Optional,
    Sequence,
    Union,
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
    flags_to_list,
    fromPSK,
    generate_channel_hash,
    message_to_json,
    pskToString,
    stripnl,
    to_node_num,
)

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

# Validation error messages for setOwner
EMPTY_LONG_NAME_MSG = "Long Name cannot be empty or contain only whitespace characters"
EMPTY_SHORT_NAME_MSG = (
    "Short Name cannot be empty or contain only whitespace characters"
)


class Node:
    """A model of a (local or remote) node in the mesh.

    Includes methods for localConfig, moduleConfig and channels
    """

    def __init__(
        self,
        iface: "MeshInterface",
        nodeNum: Union[int, str],
        noProto: bool = False,
        timeout: float = 300.0,
    ) -> None:
        """
        Create and initialize a Node instance that holds configuration, channel state, and runtime flags for a mesh node.

        Parameters
        ----------
            iface (MeshInterface): Interface used for network I/O and device interactions.
            nodeNum (int | str): Node identifier (numeric or string convertible to a node number).
            noProto (bool): If True, protocol-based operations are disabled for this node.
            timeout (float): Maximum seconds used for operations that wait for responses.

        """
        self.iface = iface
        self.nodeNum = to_node_num(nodeNum) if isinstance(nodeNum, str) else nodeNum
        self.localConfig = localonly_pb2.LocalConfig()
        self.moduleConfig = localonly_pb2.LocalModuleConfig()
        self.channels: Optional[List[channel_pb2.Channel]] = None
        self._timeout = Timeout(maxSecs=timeout)
        self.partialChannels: List[channel_pb2.Channel] = []
        self.noProto = noProto
        self.cannedPluginMessage: Optional[str] = None
        self.cannedPluginMessageMessages: Optional[str] = None
        self.ringtone: Optional[str] = None
        self.ringtonePart: Optional[str] = None

    def __repr__(self) -> str:
        """
        Return a developer-oriented string representing the Node including its interface, node number, and active non-default flags.

        Returns
        -------
            str: A debug-friendly representation containing the iface repr, the node number
                formatted as eight-hex digits (prefixed with '0x'), and any non-default flags such
                as `noProto` or a non-default `timeout`.

        """
        r = f"Node({self.iface!r}, 0x{self.nodeNum:08x}"
        if self.noProto:
            r += ", noProto=True"
        if self._timeout.expireTimeout != 300.0:
            r += f", timeout={self._timeout.expireTimeout!r}"
        r += ")"
        return r

    @staticmethod
    def position_flags_list(position_flags: int) -> List[str]:
        """
        Convert a PositionConfig position flags bitfield into a list of flag names.

        Parameters
        ----------
            position_flags (int): Bitfield of flags from Config.PositionConfig.PositionFlags.

        Returns
        -------
            list[str]: Names of the flags set in `position_flags`.

        """
        return flags_to_list(
            config_pb2.Config.PositionConfig.PositionFlags, position_flags
        )

    @staticmethod
    def excluded_modules_list(excluded_modules: int) -> List[str]:
        """
        Convert an ExcludedModules bitfield into a list of excluded module names.

        Parameters
        ----------
            excluded_modules (int): Bitfield using values from mesh_pb2.ExcludedModules.

        Returns
        -------
            List[str]: Names of modules whose bits are set in the bitfield.

        """
        return flags_to_list(mesh_pb2.ExcludedModules, excluded_modules)

    def module_available(self, excluded_bit: int) -> bool:
        """
        Determine whether a specific module bit is allowed by the interface metadata.

        Parameters
        ----------
            excluded_bit (int): Bit mask for a module as defined in DeviceMetadata.excluded_modules.

        Returns
        -------
            bool: `True` if the bit is not set in the interface metadata (module available), or if
                metadata is missing or an error occurs; `False` if the bit is set (module excluded).

        """
        meta = getattr(self.iface, "metadata", None)
        if meta is None:
            return True
        try:
            return (meta.excluded_modules & excluded_bit) == 0
        except Exception:
            return True

    def showChannels(self) -> None:
        """
        Print a human-readable list of configured channels and their shareable URLs.

        Each non-disabled channel is printed with its index, role, masked PSK, and settings as JSON.
        After listing channels, the primary channel URL is printed; if the full URL that includes
        all channels differs, it is printed as the "Complete URL".
        """
        print("Channels:")
        if self.channels:
            logger.debug(f"self.channels:{self.channels}")
            for c in self.channels:
                cStr = message_to_json(c.settings)
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
        """
        Print the node's local and module configuration and its channels to standard output.

        If present, the local and module configurations are printed as JSON; channel information is printed afterward.
        """
        prefs = ""
        if self.localConfig:
            prefs = message_to_json(self.localConfig, multiline=True)
        print(f"Preferences: {prefs}\n")
        prefs = ""
        if self.moduleConfig:
            prefs = message_to_json(self.moduleConfig, multiline=True)
        print(f"Module preferences: {prefs}\n")
        self.showChannels()

    def setChannels(self, channels: Sequence[channel_pb2.Channel]) -> None:
        """
        Set the node's channel list and normalize channel entries.

        Parameters
        ----------
            channels (list): Sequence of channel protobufs to assign to this node. The assigned
                list will be normalized (indices fixed) and padded as needed to meet expected
                channel count.

        """
        self.channels = list(channels)
        self._fixupChannels()

    def requestChannels(self, startingIndex: int = 0) -> None:
        """
        Request channel definitions from the node, starting at the given channel index.

        When called with startingIndex 0, clears any cached channels and begins a fresh fetch into
        an internal partialChannels list. The method initiates the network request for the
        specified channel index.

        Parameters
        ----------
            startingIndex (int): Zero-based channel index to start fetching from (typically 0-7).

        """
        logger.debug(f"requestChannels for nodeNum:{self.nodeNum}")
        # only initialize if we're starting out fresh
        if startingIndex == 0:
            self.channels = None
            # We keep our channels in a temp array until finished
            self.partialChannels = []
        self._requestChannel(startingIndex)

    def onResponseRequestSettings(self, p):
        """
        Process an admin response for a settings request and update the node's config objects.

        Parses the decoded response packet `p` to determine whether the request was acknowledged or
        rejected, marks the interface acknowledgment flags accordingly, and if the response contains
        `getConfigResponse` or `getModuleConfigResponse` copies the returned raw config into
        `self.localConfig` or `self.moduleConfig` respectively and prints the populated field.

        Parameters
        ----------
            p (dict): Decoded response packet containing at least a `"decoded"` mapping with
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
                field = list(resp.keys())[0]
                config_type = self.localConfig.DESCRIPTOR.fields_by_name.get(
                    camel_to_snake(field)
                )
                if config_type is not None:
                    config_values = getattr(self.localConfig, config_type.name)
            elif "getModuleConfigResponse" in adminMessage:
                oneof = "get_module_config_response"
                resp = adminMessage["getModuleConfigResponse"]
                field = list(resp.keys())[0]
                config_type = self.moduleConfig.DESCRIPTOR.fields_by_name.get(
                    camel_to_snake(field)
                )
                if config_type is not None:
                    config_values = getattr(self.moduleConfig, config_type.name)
            else:
                print(
                    "Did not receive a valid response. Make sure to have a shared channel named 'admin'."
                )
                return
            if config_values is not None:
                raw_config = getattr(
                    getattr(adminMessage["raw"], oneof), camel_to_snake(field)
                )
                config_values.CopyFrom(raw_config)
                print(f"{camel_to_snake(field)!s}:\n{config_values!s}")

    def requestConfig(self, configType: Any) -> None:
        """
        Request a configuration subset or whole configuration from the node via an admin message.

        If configType is an integer, it is treated as a config index to request. If configType is
        a protobuf field descriptor (an object with an `index` and a `containing_type.name`), the
        descriptor's `index` is used and the request targets LocalConfig when
        `containing_type.name == "LocalConfig"`, otherwise it targets the module config. For the
        local node the request is sent without an on-response handler; for remote nodes the method
        registers a response handler and waits for an ACK/NAK before returning.

        Parameters
        ----------
            configType (int or protobuf field descriptor): The config identifier to request; either
                a numeric config index or a protobuf field descriptor indicating which config field
                to fetch.

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
            msgIndex = configType.index
            if configType.containing_type.name == "LocalConfig":
                p.get_config_request = msgIndex  # type: ignore[assignment] # pyright: ignore[reportAttributeAccessIssue]
            else:
                p.get_module_config_request = msgIndex  # type: ignore[assignment] # pyright: ignore[reportAttributeAccessIssue]

        self._sendAdmin(p, wantResponse=True, onResponse=onResponse)
        if onResponse:
            self.iface.waitForAckNak()

    def turnOffEncryptionOnPrimaryChannel(self):
        """
        Disable encryption on the primary channel and write the updated channel to the device.

        Raises
        ------
            MeshInterfaceError: if channel data has not been loaded.

        """
        if self.channels is None:
            self._raise_interface_error("Error: No channels have been read")
        self.channels[0].settings.psk = fromPSK("none")
        logger.info("Writing modified channels to device")
        self.writeChannel(0)

    def waitForConfig(self, attribute: str = "channels") -> bool:
        """
        Block until the node's specified configuration attribute under localConfig is populated.

        Parameters
        ----------
            attribute (str): Name of the attribute on `localConfig` to wait for (default: "channels").

        Returns
        -------
            bool: `True` if the attribute was set before the timeout expired, `False` otherwise.

        """
        return self._timeout.waitForSet(self, attrs=("localConfig", attribute))

    def _raise_interface_error(self, message: str) -> NoReturn:
        """
        Raise a MeshInterface-style error with the provided message.

        Parameters
        ----------
            message (str): The error message to use for the raised MeshInterfaceError.

        Raises
        ------
            MeshInterface.MeshInterfaceError: Always raised with the provided message.

        """
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        raise MeshInterface.MeshInterfaceError(message)

    def writeConfig(self, config_name: str) -> None:
        """
        Write a named subset of the node's edited configuration to the device.

        This sends only the specified configuration section (device, position, power, network,
        display, lora, bluetooth, security) or module configuration section (mqtt, serial,
        external_notification, store_forward, range_test, telemetry, canned_message, audio,
        remote_hardware, neighbor_info, detection_sensor, ambient_lighting, paxcounter)
        to the target node. For remote nodes the send expects an acknowledgment; for the
        local node the message is sent without waiting for an ACK/NAK.

        Parameters
        ----------
            config_name (str): The name of the configuration section to write. Must be one of:
                "device", "position", "power", "network", "display", "lora", "bluetooth",
                "security", "mqtt", "serial", "external_notification", "store_forward",
                "range_test", "telemetry", "canned_message", "audio", "remote_hardware",
                "neighbor_info", "detection_sensor", "ambient_lighting", "paxcounter".

        Raises
        ------
            MeshInterfaceError: If config_name is not one of the supported names.

        """
        p = admin_pb2.AdminMessage()

        if config_name == "device":
            p.set_config.device.CopyFrom(self.localConfig.device)
        elif config_name == "position":
            p.set_config.position.CopyFrom(self.localConfig.position)
        elif config_name == "power":
            p.set_config.power.CopyFrom(self.localConfig.power)
        elif config_name == "network":
            p.set_config.network.CopyFrom(self.localConfig.network)
        elif config_name == "display":
            p.set_config.display.CopyFrom(self.localConfig.display)
        elif config_name == "lora":
            p.set_config.lora.CopyFrom(self.localConfig.lora)
        elif config_name == "bluetooth":
            p.set_config.bluetooth.CopyFrom(self.localConfig.bluetooth)
        elif config_name == "security":
            p.set_config.security.CopyFrom(self.localConfig.security)
        elif config_name == "mqtt":
            p.set_module_config.mqtt.CopyFrom(self.moduleConfig.mqtt)
        elif config_name == "serial":
            p.set_module_config.serial.CopyFrom(self.moduleConfig.serial)
        elif config_name == "external_notification":
            p.set_module_config.external_notification.CopyFrom(
                self.moduleConfig.external_notification
            )
        elif config_name == "store_forward":
            p.set_module_config.store_forward.CopyFrom(self.moduleConfig.store_forward)
        elif config_name == "range_test":
            p.set_module_config.range_test.CopyFrom(self.moduleConfig.range_test)
        elif config_name == "telemetry":
            p.set_module_config.telemetry.CopyFrom(self.moduleConfig.telemetry)
        elif config_name == "canned_message":
            p.set_module_config.canned_message.CopyFrom(
                self.moduleConfig.canned_message
            )
        elif config_name == "audio":
            p.set_module_config.audio.CopyFrom(self.moduleConfig.audio)
        elif config_name == "remote_hardware":
            p.set_module_config.remote_hardware.CopyFrom(
                self.moduleConfig.remote_hardware
            )
        elif config_name == "neighbor_info":
            p.set_module_config.neighbor_info.CopyFrom(self.moduleConfig.neighbor_info)
        elif config_name == "detection_sensor":
            p.set_module_config.detection_sensor.CopyFrom(
                self.moduleConfig.detection_sensor
            )
        elif config_name == "ambient_lighting":
            p.set_module_config.ambient_lighting.CopyFrom(
                self.moduleConfig.ambient_lighting
            )
        elif config_name == "paxcounter":
            p.set_module_config.paxcounter.CopyFrom(self.moduleConfig.paxcounter)
        else:
            self._raise_interface_error(
                f"Error: No valid config with name {config_name}"
            )

        logger.debug(f"Wrote: {config_name}")
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        self._sendAdmin(p, onResponse=onResponse)

    def writeChannel(self, channelIndex: int, adminIndex: int = 0) -> None:
        """
        Write the channel at the given index to the device.

        Sends the specified channel configuration to the node and ensures an admin session key is present before sending.

        Parameters
        ----------
            channelIndex (int): Index of the channel to write.
            adminIndex (int): Admin channel index to use for sending.

        Raises
        ------
            MeshInterfaceError: If channels have not been loaded (no channels to write).

        """
        if self.channels is None:
            self._raise_interface_error("Error: No channels have been read")
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.set_channel.CopyFrom(self.channels[channelIndex])
        self._sendAdmin(p, adminIndex=adminIndex)
        logger.debug(f"Wrote channel {channelIndex}")

    def getChannelByChannelIndex(
        self, channelIndex: int
    ) -> Optional[channel_pb2.Channel]:
        """
        Retrieve the channel at the given zero-based index from this node's channels.

        Parameters
        ----------
            channelIndex (int): Zero-based channel index (typically 0-7).

        Returns
        -------
            channel_pb2.Channel or None: The channel at the specified index, or None if channels are unset or the index is out of range.

        """
        ch = None
        if self.channels and 0 <= channelIndex < len(self.channels):
            ch = self.channels[channelIndex]
        return ch

    def deleteChannel(self, channelIndex: int) -> None:
        """
        Delete the channel at channelIndex and shift subsequent channels down to fill the gap.

        Parameters
        ----------
            channelIndex (int): Index of the channel to delete.

        Raises
        ------
            MeshInterfaceError: If channels have not been loaded.
            MeshInterfaceError: If the channel at channelIndex is not Role.SECONDARY or Role.DISABLED.

        Description:
            Removes the channel, normalizes the channel list back to the device's channel count, and
            rewrites affected channels to the device. When operating on the local node, the method
            adjusts admin-channel handling so ongoing writes use the correct admin index.

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
        adminIndex = self.iface.localNode._getAdminChannelIndex()

        self.channels.pop(channelIndex)
        self._fixupChannels()  # expand back to 8 channels

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

    def getChannelByName(self, name: str) -> Optional[channel_pb2.Channel]:
        """
        Find a channel whose settings.name exactly matches the provided name.

        Returns
        -------
            The matching channel object if found, `None` otherwise.

        """
        for c in self.channels or []:
            if c.settings and c.settings.name == name:
                return c
        return None

    def getDisabledChannel(self) -> Optional[channel_pb2.Channel]:
        """
        Return the first channel whose role is DISABLED.

        Returns
        -------
            channel (Channel | None): The first disabled Channel object if present, otherwise `None`.

        """
        if self.channels is None:
            return None
        for c in self.channels:
            if c.role == channel_pb2.Channel.Role.DISABLED:
                return c
        return None

    def _getAdminChannelIndex(self) -> int:
        """
        Get the index of the channel named "admin", or 0 if no such channel exists.

        Returns
        -------
            int: Index of the admin channel, or 0 if no channel with name "admin" is present.

        """
        for c in self.channels or []:
            if c.settings and c.settings.name.lower() == "admin":
                return c.index
        return 0

    def setOwner(
        self,
        long_name: Optional[str] = None,
        short_name: Optional[str] = None,
        is_licensed: bool = False,
        is_unmessagable: Optional[bool] = None,
    ) -> Optional[mesh_pb2.MeshPacket]:
        """
        Set the device owner fields (long and short names) and license/unmessagable flags on this node.

        Parameters
        ----------
            long_name (Optional[str]): Owner long name; leading/trailing whitespace is trimmed. If
                provided and empty after trimming, a ValueError is raised.
            short_name (Optional[str]): Owner short name; leading/trailing whitespace is trimmed.
                If provided and longer than 4 characters it will be truncated to 4 characters. If
                empty after trimming, a ValueError is raised.
            is_licensed (bool): Whether the owner is licensed; applied when `long_name` is provided.
            is_unmessagable (Optional[bool]): If provided, sets the owner's unmessagable flag.

        Returns
        -------
            mesh_pb2.MeshPacket or None: The sent Admin message packet if available, otherwise None.

        Raises
        ------
            ValueError: If `long_name` or `short_name` is provided but empty or whitespace-only after trimming.

        """
        logger.debug(f"in setOwner nodeNum:{self.nodeNum}")
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()

        nChars = 4
        if long_name is not None:
            long_name = long_name.strip()
            # Validate that long_name is not empty or whitespace-only
            if not long_name:
                raise ValueError(EMPTY_LONG_NAME_MSG)
            p.set_owner.long_name = long_name
            p.set_owner.is_licensed = is_licensed
        if short_name is not None:
            short_name = short_name.strip()
            # Validate that short_name is not empty or whitespace-only
            if not short_name:
                raise ValueError(EMPTY_SHORT_NAME_MSG)
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
        logger.debug(f"p.set_owner.is_licensed:{p.set_owner.is_licensed}")
        logger.debug(f"p.set_owner.is_unmessagable:{p.set_owner.is_unmessagable}:")
        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def getURL(self, includeAll: bool = True) -> str:
        """
        Build a sharable meshtastic URL encoding the node's primary channel and LoRa configuration.

        Includes the secondary channels in the URL when requested.

        Parameters
        ----------
                includeAll (bool): If True, include secondary channels in addition to the primary channel.

        Returns
        -------
                share_url (str): A meshtastic.org URL containing the encoded channel set and LoRa configuration.

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
        """
        Parse a Mesh URL and apply its channel and LoRa configuration to this node.

        If addOnly is False, replace the node's channel list with the channels encoded in the URL
        (first becomes PRIMARY, subsequent become SECONDARY) and write each channel to the device.
        If addOnly is True, add only channels from the URL whose names are not already present,
        placing each into the first available DISABLED channel and writing it.

        Parameters
        ----------
            url (str): A Mesh share URL containing a base64-encoded ChannelSet (e.g., .../#<base64> or .../?add=true#<base64>).
            addOnly (bool): If True, add channels without modifying existing ones; if False, replace channels with those from the URL.

        Raises
        ------
            MeshInterfaceError: If channels or configuration are not loaded, the URL is invalid or
                contains no settings, or no free channel slot is available when adding.

        """
        if self.channels is None:
            self._raise_interface_error("Warning: config or channels not loaded")

        # URLs are of the form https://meshtastic.org/d/#{base64_channel_set}
        # Split on '/#' to find the base64 encoded channel settings
        if addOnly:
            splitURL = url.split("/?add=true#")
        else:
            splitURL = url.split("/#")
        if len(splitURL) == 1:
            self._raise_interface_error(f"Warning: Invalid URL '{url}'")
        b64 = splitURL[-1]

        # We normally strip padding to make for a shorter URL, but the python parser doesn't like
        # that.  So add back any missing padding
        # per https://stackoverflow.com/a/9807138
        missing_padding = len(b64) % 4
        if missing_padding:
            b64 += "=" * (4 - missing_padding)

        try:
            decodedURL = base64.urlsafe_b64decode(b64)
        except (binascii.Error, ValueError) as ex:
            self._raise_interface_error(f"Warning: Invalid URL '{url}': {ex}")
        channelSet = apponly_pb2.ChannelSet()
        try:
            channelSet.ParseFromString(decodedURL)
        except (DecodeError, ValueError) as ex:
            self._raise_interface_error(
                f"Warning: Unable to parse channel settings from URL '{url}': {ex}"
            )

        if len(channelSet.settings) == 0:
            self._raise_interface_error("Warning: There were no settings.")

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
                    self._raise_interface_error("Warning: No free channels were found")
                ch.settings.CopyFrom(chs)
                ch.role = channel_pb2.Channel.Role.SECONDARY
                logger.info(f"Adding new channel '{chs.name}' to device")
                self.writeChannel(ch.index)
        else:
            i = 0
            max_channels = len(self.channels)
            for chs in channelSet.settings:
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
                i += 1

        p = admin_pb2.AdminMessage()
        p.set_config.lora.CopyFrom(channelSet.lora_config)
        self.ensureSessionKey()
        self._sendAdmin(p)

    def onResponseRequestRingtone(self, p: Dict[str, Any]) -> None:
        """
        Handle an incoming admin response containing a ringtone part and record it on the Node.

        Checks the decoded packet for routing errors; if none are present and the packet contains
        an admin.raw get_ringtone_response, stores that value in self.ringtonePart.
        If a routing error is present, prints the error reason.

        Parameters
        ----------
            p (dict): Decoded response packet structure from the interface. Expected shape includes optional keys:
                - "decoded": dict containing response fields
                - "decoded"]["routing"]["errorReason"]: routing error string (e.g., "NONE")
                - "decoded"]["admin"]["raw"].get_ringtone_response: ringtone part payload to store

        """
        logger.debug(f"onResponseRequestRingtone() p:{p}")
        errorFound = False
        if "routing" in p["decoded"]:
            if p["decoded"]["routing"]["errorReason"] != "NONE":
                errorFound = True
                logger.error(
                    f"Error on response: {p['decoded']['routing']['errorReason']}"
                )
        if errorFound is False:
            if "decoded" in p:
                if "admin" in p["decoded"]:
                    if "raw" in p["decoded"]["admin"]:
                        self.ringtonePart = p["decoded"]["admin"][
                            "raw"
                        ].get_ringtone_response
                        logger.debug(f"self.ringtonePart:{self.ringtonePart}")

    def get_ringtone(self) -> Optional[str]:
        """
        Retrieve the node's ringtone as a single concatenated string.

        This may block while waiting for the device to respond. If the External Notification
        module is excluded by firmware, no request is made and the method returns None.

        Returns
        -------
            ringtone (str | None): The complete ringtone string if available, `None` if the module is not present or ringtone is unavailable.

        """
        logger.debug("in get_ringtone()")
        if not self.module_available(mesh_pb2.EXTNOTIF_CONFIG):
            logger.warning(
                "External Notification module not present (excluded by firmware)"
            )
            return None

        if not self.ringtone:
            response_event = threading.Event()

            def _on_ringtone_response(packet: Dict[str, Any]) -> None:
                try:
                    self.onResponseRequestRingtone(packet)
                finally:
                    response_event.set()

            p1 = admin_pb2.AdminMessage()
            p1.get_ringtone_request = True
            self._sendAdmin(p1, wantResponse=True, onResponse=_on_ringtone_response)
            if not response_event.wait(timeout=self._timeout.expireTimeout):
                logger.warning("Timed out waiting for ringtone response")
                return None

            logger.debug(f"self.ringtone:{self.ringtone}")

            self.ringtone = ""
            if self.ringtonePart:
                self.ringtone += self.ringtonePart

        logger.debug(f"ringtone:{self.ringtone}")
        return self.ringtone

    def set_ringtone(self, ringtone: str) -> Optional[mesh_pb2.MeshPacket]:
        """
        Set the node's ringtone.

        Validates that the External Notification module is available and that the ringtone length
        is 230 characters or fewer; ensures an admin session key, then sends one admin message.
        Returns None if the External Notification module is not available. For remote nodes the
        send waits for an ACK/NAK response.

        Parameters
        ----------
            ringtone (str): The ringtone text to set.

        Returns
        -------
            send_result: The result of sending the AdminMessage for the first chunk, or `None` if the External Notification module is unavailable.

        Raises
        ------
            MeshInterfaceError: If `ringtone` length exceeds 230 characters.

        """
        if not self.module_available(mesh_pb2.EXTNOTIF_CONFIG):
            logger.warning(
                "External Notification module not present (excluded by firmware)"
            )
            return None

        if len(ringtone) > 230:
            self._raise_interface_error(
                "Warning: The ringtone must be less than 230 characters."
            )
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.set_ringtone_message = ringtone

        logger.debug("Setting ringtone '%s'", ringtone)
        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def onResponseRequestCannedMessagePluginMessageMessages(
        self, p: Dict[str, Any]
    ) -> None:
        """
        Handle the admin response for a canned-message plugin messages request.

        If the response indicates a routing error, prints the error. On a successful response,
        stores the `get_canned_message_module_messages_response` payload from the admin raw data
        into `self.cannedPluginMessageMessages`.

        Parameters
        ----------
            p (dict): Decoded packet dictionary containing response fields, expected to include
                keys like `"decoded"`, `"decoded"]["routing"]`, and `"decoded"]["admin"]["raw"]`.

        """
        logger.debug(f"onResponseRequestCannedMessagePluginMessageMessages() p:{p}")
        errorFound = False
        if "routing" in p["decoded"]:
            if p["decoded"]["routing"]["errorReason"] != "NONE":
                errorFound = True
                logger.error(
                    f"Error on response: {p['decoded']['routing']['errorReason']}"
                )
        if errorFound is False:
            if "decoded" in p:
                if "admin" in p["decoded"]:
                    if "raw" in p["decoded"]["admin"]:
                        self.cannedPluginMessageMessages = p["decoded"]["admin"][
                            "raw"
                        ].get_canned_message_module_messages_response
                        logger.debug(
                            f"self.cannedPluginMessageMessages:{self.cannedPluginMessageMessages}"
                        )

    def get_canned_message(self) -> Optional[str]:
        """
        Retrieve the device's canned message, requesting parts from the node if not already cached.

        Blocks until the device responds when a request is made.

        Returns
        -------
            str: The assembled canned message, or `None` if the canned message module is unavailable.

        """
        logger.debug("in get_canned_message()")
        if not self.module_available(mesh_pb2.CANNEDMSG_CONFIG):
            logger.warning("Canned Message module not present (excluded by firmware)")
            return None
        if not self.cannedPluginMessage:
            response_event = threading.Event()

            def _on_canned_message_response(packet: Dict[str, Any]) -> None:
                try:
                    self.onResponseRequestCannedMessagePluginMessageMessages(packet)
                finally:
                    response_event.set()

            p1 = admin_pb2.AdminMessage()
            p1.get_canned_message_module_messages_request = True
            self._sendAdmin(
                p1,
                wantResponse=True,
                onResponse=_on_canned_message_response,
            )
            if not response_event.wait(timeout=self._timeout.expireTimeout):
                logger.warning("Timed out waiting for canned message response")
                return None

            logger.debug(
                f"self.cannedPluginMessageMessages:{self.cannedPluginMessageMessages}"
            )

            self.cannedPluginMessage = ""
            if self.cannedPluginMessageMessages:
                self.cannedPluginMessage += self.cannedPluginMessageMessages

        logger.debug(f"canned_plugin_message:{self.cannedPluginMessage}")
        return self.cannedPluginMessage

    def set_canned_message(self, message: str) -> Optional[mesh_pb2.MeshPacket]:
        """
        Set the device's canned message.

        If the canned-message module is not available on the device, the method logs a warning and
        returns None. If the provided message is longer than 200 characters, a MeshInterfaceError
        is raised. The message is sent with one admin request (waiting for an ACK/NAK when
        targeting a remote node).

        Parameters
        ----------
            message (str): The canned message to set; must be 200 characters or fewer.

        Returns
        -------
            The result returned by _sendAdmin for the first chunk, or `None` if the canned-message module is unavailable.

        Raises
        ------
            MeshInterfaceError: If `message` length is greater than 200 characters.

        """
        if not self.module_available(mesh_pb2.CANNEDMSG_CONFIG):
            logger.warning("Canned Message module not present (excluded by firmware)")
            return None

        if len(message) > 200:
            self._raise_interface_error(
                "The canned message must be less than 200 characters."
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
        return self._sendAdmin(p, onResponse=onResponse)

    def exitSimulator(self) -> Optional[mesh_pb2.MeshPacket]:
        """
        Request that the target simulator process exit; this request has no effect on non-simulator nodes.

        This will ensure an admin session key is present and send an AdminMessage with the exit_simulator flag set.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.exit_simulator = True
        logger.debug("in exitSimulator()")

        return self._sendAdmin(p)

    def reboot(self, secs: int = 10) -> Optional[mesh_pb2.MeshPacket]:
        """Tell the node to reboot."""
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.reboot_seconds = secs
        logger.info(f"Telling node to reboot in {secs} seconds")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def beginSettingsTransaction(self) -> Optional[mesh_pb2.MeshPacket]:
        """
        Open a settings edit transaction on the node.

        Ensures an admin session key exists before sending the request. For remote nodes, the call
        waits for an ACK/NAK response; for the local node it does not wait.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.begin_edit_settings = True
        logger.info("Telling open a transaction to edit settings")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def commitSettingsTransaction(self) -> Optional[mesh_pb2.MeshPacket]:
        """
        Commit the node's open settings edit transaction.

        If the node is remote, this will wait for an ACK/NAK response; for the local node the commit is sent without waiting for a response.
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
        return self._sendAdmin(p, onResponse=onResponse)

    def rebootOTA(self, secs: int = 10) -> Optional[mesh_pb2.MeshPacket]:
        """Tell the node to reboot into factory firmware."""
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.reboot_ota_seconds = secs
        logger.info(f"Telling node to reboot to OTA in {secs} seconds")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def enterDFUMode(self) -> Optional[mesh_pb2.MeshPacket]:
        """
        Request the node to enter DFU (NRF52) mode.

        Ensures an admin session key exists and sends an AdminMessage asking the node to enter DFU;
        when targeting a remote node this will wait for an ACK/NAK response.
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
        return self._sendAdmin(p, onResponse=onResponse)

    def shutdown(self, secs: int = 10) -> Optional[mesh_pb2.MeshPacket]:
        """Tell the node to shutdown."""
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.shutdown_seconds = secs
        logger.info(f"Telling node to shutdown in {secs} seconds")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def getMetadata(self) -> None:
        """
        Request the node's device metadata and wait for an acknowledgement.

        Sends a metadata request to the node and blocks until an ACK or NAK is received; the
        received metadata is processed asynchronously when the response arrives.
        """
        p = admin_pb2.AdminMessage()
        p.get_device_metadata_request = True
        logger.info("Requesting device metadata")

        self._sendAdmin(p, wantResponse=True, onResponse=self.onRequestGetMetadata)
        self.iface.waitForAckNak()

    def factoryReset(self, full: bool = False) -> Optional[mesh_pb2.MeshPacket]:
        """
        Request a factory reset on the node.

        Ensures an admin session key exists, then sends a factory-reset request to the node. If
        `full` is True, requests a full device reset; otherwise requests a configuration-only reset.
        For remote targets the send is performed with ACK/NAK response handling.

        Parameters
        ----------
            full (bool): True to perform a full device reset, False to reset configuration only.

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
        return self._sendAdmin(p, onResponse=onResponse)

    def removeNode(self, nodeId: Union[int, str]) -> Optional[mesh_pb2.MeshPacket]:
        """
        Request that this node remove the mesh node identified by nodeId.

        nodeId is converted to a numeric node number before sending. This method sends a
        remove-by-node-number admin request to the device; for remote targets it uses ACK/NAK
        handling, for the local node no response callback is used.

        Parameters
        ----------
            nodeId (int | str): Node number or a string convertible to a node number.

        """
        self.ensureSessionKey()
        nodeId = to_node_num(nodeId)

        p = admin_pb2.AdminMessage()
        p.remove_by_nodenum = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def setFavorite(self, nodeId: Union[int, str]) -> Optional[mesh_pb2.MeshPacket]:
        """Tell the node to set the specified node ID to be favorited on the NodeDB on the device."""
        self.ensureSessionKey()
        nodeId = to_node_num(nodeId)

        p = admin_pb2.AdminMessage()
        p.set_favorite_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def removeFavorite(self, nodeId: Union[int, str]) -> Optional[mesh_pb2.MeshPacket]:
        """
        Unmark a node as a favorite in the device's NodeDB.

        Parameters
        ----------
            nodeId (int | str): The node's numeric identifier or a string convertible to it.

        """
        self.ensureSessionKey()
        nodeId = to_node_num(nodeId)

        p = admin_pb2.AdminMessage()
        p.remove_favorite_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def setIgnored(self, nodeId: Union[int, str]) -> Optional[mesh_pb2.MeshPacket]:
        """
        Mark a node (by node number) as ignored in the device's NodeDB.

        Parameters
        ----------
            nodeId (int | str): Node number or string that can be converted to a node number.

        """
        self.ensureSessionKey()
        nodeId = to_node_num(nodeId)

        p = admin_pb2.AdminMessage()
        p.set_ignored_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def removeIgnored(self, nodeId: Union[int, str]) -> Optional[mesh_pb2.MeshPacket]:
        """
        Unmark a node as ignored in the device's NodeDB.

        Parameters
        ----------
            nodeId (int | str): Node identifier (numeric or string) to un-ignore; it will be converted to a numeric node number.

        Returns
        -------
            The result returned by _sendAdmin after sending the AdminMessage.

        """
        self.ensureSessionKey()
        nodeId = to_node_num(nodeId)

        p = admin_pb2.AdminMessage()
        p.remove_ignored_node = nodeId

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def resetNodeDb(self) -> Optional[mesh_pb2.MeshPacket]:
        """Tell the node to reset its list of nodes."""
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.nodedb_reset = True
        logger.info("Telling node to reset the NodeDB")

        # If sending to a remote node, wait for ACK/NAK
        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def setFixedPosition(
        self, lat: Union[int, float], lon: Union[int, float], alt: int
    ) -> Optional[mesh_pb2.MeshPacket]:
        """
        Set the node's fixed position and enable the fixed-position setting on the device.

        Parameters
        ----------
            lat (int | float): Latitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
            lon (int | float): Longitude specified either as an integer in 1e-7 degrees units or as a float in decimal degrees.
            alt (int): Altitude in meters (0 leaves altitude unset).

        Returns
        -------
            mesh_packet (Optional[mesh_pb2.MeshPacket]): The result from sending the AdminMessage, or `None` if no packet was sent.

        """
        self.ensureSessionKey()

        p = mesh_pb2.Position()
        if isinstance(lat, float) and lat != 0.0:
            p.latitude_i = int(lat / 1e-7)
        elif isinstance(lat, int) and lat != 0:
            p.latitude_i = lat

        if isinstance(lon, float) and lon != 0.0:
            p.longitude_i = int(lon / 1e-7)
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
        return self._sendAdmin(a, onResponse=onResponse)

    def removeFixedPosition(self) -> Optional[mesh_pb2.MeshPacket]:
        """
        Remove the node's fixed position setting.

        Sends an AdminMessage to clear the node's fixed position. For remote nodes, uses the ACK/NAK response handler.
        """
        self.ensureSessionKey()
        p = admin_pb2.AdminMessage()
        p.remove_fixed_position = True
        logger.info("Telling node to remove fixed position")

        if self == self.iface.localNode:
            onResponse = None
        else:
            onResponse = self.onAckNak
        return self._sendAdmin(p, onResponse=onResponse)

    def setTime(self, timeSec: int = 0) -> Optional[mesh_pb2.MeshPacket]:
        """
        Set the node's clock to a specified Unix timestamp.

        If timeSec is 0 or omitted, the system's current time is used.

        Parameters
        ----------
            timeSec (int): Unix timestamp in seconds to set on the node; pass 0 to use the current system time.

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
        return self._sendAdmin(p, onResponse=onResponse)

    def _fixupChannels(self):
        """
        Normalize the node's channel list by assigning sequential index values and ensuring the list contains the expected number of channels.

        If `channels` is None this is a no-op. Otherwise this method sets each channel's `index`
        field to its position in the list (starting at 0) and then appends disabled channels as
        needed so the channel list reaches the required length.
        """
        channels = self.channels
        if channels is None:
            return

        # Add extra disabled channels as needed
        # This is needed because the protobufs will have index **missing** if the channel number is zero
        for index, ch in enumerate(channels):
            ch.index = index  # fixup indexes

        self._fillChannels()

    def _fillChannels(self):
        """Mark unused channels as disabled."""
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

    def onRequestGetMetadata(self, p):
        """
        Handle a device metadata response packet and surface its contents.

        Parses the provided decoded packet, updates interface acknowledgment state, and either
        retries the metadata request on routing-app retry indications or prints the received device
        metadata fields (firmware_version, device_state_version, role, position_flags, hw_model,
        hasPKC, and excluded_modules). Also resets or expires the internal timeout depending on
        progress.

        Parameters
        ----------
            p (dict): Decoded packet structure containing at least a 'decoded' key with routing and
                admin/raw get_device_metadata_response fields.

        """
        logger.debug(f"onRequestGetMetadata() p:{p}")

        if "routing" in p["decoded"]:
            if p["decoded"]["routing"]["errorReason"] != "NONE":
                logger.error(
                    f"Error on response: {p['decoded']['routing']['errorReason']}"
                )
                self.iface._acknowledgment.receivedNak = True
        else:
            self.iface._acknowledgment.receivedAck = True
            if p["decoded"]["portnum"] == portnums_pb2.PortNum.Name(
                portnums_pb2.PortNum.ROUTING_APP
            ):
                if p["decoded"]["routing"]["errorReason"] != "NONE":
                    logger.warning(
                        f"Metadata request failed, error reason: {p['decoded']['routing']['errorReason']}"
                    )
                    self._timeout.expireTime = time.time()  # Do not wait any longer
                    return  # Don't try to parse this routing message
                logger.debug("Retrying metadata request.")
                self.getMetadata()
                return

            c = p["decoded"]["admin"]["raw"].get_device_metadata_response
            self._timeout.reset()  # We made forward progress
            logger.debug(f"Received metadata {stripnl(c)}")
            print(f"\nfirmware_version: {c.firmware_version}")
            print(f"device_state_version: {c.device_state_version}")
            if c.role in config_pb2.Config.DeviceConfig.Role.values():
                print(f"role: {config_pb2.Config.DeviceConfig.Role.Name(c.role)}")
            else:
                print(f"role: {c.role}")
            print(f"position_flags: {self.position_flags_list(c.position_flags)}")
            if c.hw_model in mesh_pb2.HardwareModel.values():
                print(f"hw_model: {mesh_pb2.HardwareModel.Name(c.hw_model)}")
            else:
                print(f"hw_model: {c.hw_model}")
            print(f"hasPKC: {c.hasPKC}")
            if c.excluded_modules > 0:
                print(
                    f"excluded_modules: {self.excluded_modules_list(c.excluded_modules)}"
                )

    def onResponseRequestChannel(self, p):
        """
        Process a response packet for a previously requested channel and update the Node's channel state.

        If the packet is a routing message with an error, expire the request timeout and retry the
        last requested channel. If the packet contains an admin get_channel_response, append that
        channel to the node's partial channel list, reset the request timeout, and either continue
        requesting the next channel or, when the final channel is received, replace the node's
        channels with the collected channels and normalize them.

        Parameters
        ----------
            p (dict): Decoded packet dictionary from the interface. Expected to contain either
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
            self._requestChannel(lastTried)
            return

        c = p["decoded"]["admin"]["raw"].get_channel_response
        self.partialChannels.append(c)
        self._timeout.reset()  # We made forward progress
        logger.debug(f"Received channel {stripnl(c)}")
        index = c.index

        if index >= 8 - 1:
            logger.debug("Finished downloading channels")

            self.channels = self.partialChannels
            self._fixupChannels()
        else:
            self._requestChannel(index + 1)

    def onAckNak(self, p):
        """
        Handle an incoming ACK/NAK admin response and update interface acknowledgment state.

        Inspect the routing error reason in the parsed packet `p` and:
        - If the errorReason is not "NONE", print a NAK message and set
          iface._acknowledgment.receivedNak to True.
        - If the errorReason is "NONE" and the packet originates from the local node, print an
          implicit-ACK message and set iface._acknowledgment.receivedImplAck to True.
        - Otherwise print a normal ACK message and set iface._acknowledgment.receivedAck to True.

        Parameters
        ----------
            p (dict): Parsed packet dictionary expected to contain:
                - p["decoded"]["routing"]["errorReason"]: routing error reason string.
                - p["from"]: numeric origin node identifier (string or int convertible).

        """
        if p["decoded"]["routing"]["errorReason"] != "NONE":
            logger.warning(
                f"Received a NAK, error reason: {p['decoded']['routing']['errorReason']}"
            )
            self.iface._acknowledgment.receivedNak = True
        else:
            if int(p["from"]) == self.iface.localNode.nodeNum:
                logger.info(
                    "Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed."
                )
                self.iface._acknowledgment.receivedImplAck = True
            else:
                logger.info("Received an ACK.")
                self.iface._acknowledgment.receivedAck = True

    def _requestChannel(self, channelNum: int):
        """
        Request the settings for a single channel from this node.

        Parameters
        ----------
            channelNum (int): Zero-based index of the channel to request.

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

        return self._sendAdmin(
            p, wantResponse=True, onResponse=self.onResponseRequestChannel
        )

    # pylint: disable=R1710
    def _sendAdmin(
        self,
        p: admin_pb2.AdminMessage,
        wantResponse: bool = False,
        onResponse: Optional[Callable[[Dict[str, Any]], Any]] = None,
        adminIndex: int = 0,
    ) -> Optional[mesh_pb2.MeshPacket]:
        """
        Send an AdminMessage to this node (local or remote) using the node's admin channel.

        Parameters
        ----------
            p (admin_pb2.AdminMessage): The AdminMessage to send; may have a session passkey attached automatically.
            wantResponse (bool): If true, request a response from the recipient.
            onResponse (callable | None): Optional callback to handle an incoming response packet.
            adminIndex (int): Channel index to use for the admin message; if 0, the node's configured admin channel will be used.

        Returns
        -------
            The result returned by iface.sendData when the message is sent, or None when sending is skipped because protocol use is disabled.

        """

        if self.noProto:
            logger.warning(
                "Not sending packet because protocol use is disabled by noProto"
            )
            return None
        else:
            if (
                adminIndex == 0
            ):  # unless a special channel index was used, we want to use the admin index
                adminIndex = self.iface.localNode._getAdminChannelIndex()
            logger.debug(f"adminIndex:{adminIndex}")
            nodeid = to_node_num(self.nodeNum)
            node_info = self.iface._getOrCreateByNum(nodeid)
            if "adminSessionPassKey" in node_info:
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

    def ensureSessionKey(self):
        """
        Ensure an admin session key exists for this node, requesting one if missing.

        If protocol use is disabled (`noProto`), no action is taken. Otherwise, if the node has no
        `adminSessionPassKey` recorded, a session-key request is sent.
        """
        if self.noProto:
            logger.warning(
                "Not ensuring session key, because protocol use is disabled by noProto"
            )
        else:
            nodeid = to_node_num(self.nodeNum)
            if self.iface._getOrCreateByNum(nodeid).get("adminSessionPassKey") is None:
                self.requestConfig(admin_pb2.AdminMessage.SESSIONKEY_CONFIG)

    def get_channels_with_hash(self) -> List[Dict[str, Any]]:
        """
        Provide channel entries with index, role, name, and a computed hash.

        Returns
        -------
            list[dict]: A list of dictionaries, each with keys:
                - "index" (int): Channel index.
                - "role" (str): Channel role name.
                - "name" (str): Channel settings name (empty string if missing).
                - "hash" (int or None): Computed channel hash when name and PSK are present, otherwise None.

        """
        result: List[Dict[str, Any]] = []
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

    def getChannelsWithHash(self) -> List[Dict[str, Any]]:
        """
        Return channel entries with hashes via the camelCase compatibility wrapper.
        """
        return self.get_channels_with_hash()
