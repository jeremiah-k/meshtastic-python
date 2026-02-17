"""Main Meshtastic."""

# We just hit the 1600 line limit for main.py, but I currently have a huge set of powermon/structured logging changes
# later we can have a separate changelist to refactor main.py into smaller files
# pylint: disable=R0917,C0302

import argparse
import importlib
import logging
import os
import platform
import sys
import time
from types import ModuleType
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml
from google.protobuf.json_format import MessageToDict
from pubsub import pub  # type: ignore[import-untyped]

import meshtastic.serial_interface
import meshtastic.tcp_interface
import meshtastic.util
from meshtastic import BROADCAST_ADDR, mt_config, remote_hardware
from meshtastic.ble_interface import BLEInterface
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2, portnums_pb2
from meshtastic.version import get_active_version

argcomplete: Optional[ModuleType] = None
try:
    import argcomplete as _argcomplete  # type: ignore

    argcomplete = _argcomplete
except ImportError:
    pass

pyqrcode: Optional[ModuleType] = None
try:
    import pyqrcode as _pyqrcode  # type: ignore[import-untyped]

    pyqrcode = _pyqrcode
except ImportError:
    pass

meshtastic_test: Optional[ModuleType] = None
try:
    meshtastic_test = importlib.import_module("meshtastic.test")
except ImportError:
    pass

try:
    powermon_module = importlib.import_module("meshtastic.powermon")
    slog_module = importlib.import_module("meshtastic.slog")
    PowerMeter = powermon_module.PowerMeter
    PowerStress = powermon_module.PowerStress
    PPK2PowerSupply = powermon_module.PPK2PowerSupply
    RidenPowerSupply = powermon_module.RidenPowerSupply
    SimPowerSupply = powermon_module.SimPowerSupply
    LogSet = slog_module.LogSet

    have_powermon = True
    powermon_exception = None
    meter = None
except (ImportError, AttributeError) as exc:
    have_powermon = False
    powermon_exception = exc
    meter = None
    logging.getLogger(__name__).debug("powermon/slog not available: %s", exc)

logger = logging.getLogger(__name__)


def onReceive(packet: Dict[str, Any], interface: MeshInterface) -> None:
    """
    Handle an incoming mesh packet and perform optional reply or exit actions based on CLI options.
    
    If the packet contains a decoded payload and the CLI was invoked to send text, this may close the interface when the packet is a text-message reply addressed to this node. If the CLI was invoked with reply enabled and the decoded payload contains text, send a reply containing the received text, the packet's rxSnr, and hopLimit.
    
    Parameters:
        packet (dict): Incoming packet dictionary; expected keys used: "decoded" (dict or None), "to" (destination node number), "rxSnr", and "hopLimit".
        interface (MeshInterface): Interface instance used to send replies and to close the connection.
    """
    args = mt_config.args
    try:
        d = packet.get("decoded")
        logger.debug(f"in onReceive() d:{d}")

        # Exit once we receive a reply
        is_text_reply = (
            args
            and args.sendtext
            and d is not None
            and interface.myInfo is not None
            and packet["to"] == interface.myInfo.my_node_num
            and d.get("portnum", portnums_pb2.PortNum.UNKNOWN_APP)
            == portnums_pb2.PortNum.TEXT_MESSAGE_APP
        )
        if is_text_reply:
            interface.close()  # after running command then exit

        # Reply to every received message with some stats
        if d is not None and args and args.reply:
            msg = d.get("text")
            if msg:
                rxSnr = packet["rxSnr"]
                hopLimit = packet["hopLimit"]
                print(f"message: {msg}")
                reply = f"got msg '{msg}' with rxSnr: {rxSnr} and hopLimit: {hopLimit}"
                print("Sending reply: ", reply)
                interface.sendText(reply)

    except Exception as ex:
        print(f"Warning: Error processing received packet: {ex}.")


def onConnection(interface: MeshInterface, topic: Any = pub.AUTO_TOPIC) -> None:
    """
    Handle notification when the connection state with a radio changes.
    
    Parameters:
        interface (MeshInterface): The interface whose connection state changed.
        topic (Any): Pub/sub topic or identifier for the event; if the object provides a `getName()` method, that name will be used for display.
    """
    _ = interface
    topic_name = topic.getName() if hasattr(topic, "getName") else str(topic)
    print(f"Connection changed: {topic_name}")


def checkChannel(interface: MeshInterface, channelIndex: int) -> bool:
    """
    Determine whether the local node has the channel at the given index enabled.
    
    Returns:
        `true` if the channel exists and its role is not DISABLED, `false` otherwise.
    """
    ch = interface.localNode.getChannelByChannelIndex(channelIndex)
    logger.debug(f"ch:{ch}")
    return bool(ch and ch.role != channel_pb2.Channel.Role.DISABLED)


def getPref(node, comp_name) -> bool:
    """
    Retrieve and display a channel or preference value for the given node.
    
    If the requested preference exists in the node's local or module configuration, print the preference name and value(s).
    If the preference is present locally, its current value(s) are printed; if the preference is not set locally but the field exists, a config request is sent to the remote node. If the preference name refers to an entire message (compound name), all populated subfields are printed.
    
    Parameters:
        node: The node object containing `localConfig` and `moduleConfig`.
        comp_name (str): Dot-separated preference name (e.g., "channel.label" or "label"); if only one name is provided it is used for both parts.
    
    Returns:
        bool: `true` if the preference field exists and the function either printed local values or requested the remote config, `false` if the field was not found.
    """

    def _printSetting(config_type, uni_name, pref_value, repeated):
        """
        Prints a configuration preference and its value to stdout and the debug log.
        
        Parameters:
            config_type: An object with a `name` attribute identifying the configuration section (e.g., a protobuf config enum or type).
            uni_name (str): The preference name within the configuration section.
            pref_value: The preference value to print; if `repeated` is True this should be an iterable of values.
            repeated (bool): If True, treat `pref_value` as a sequence and print a list of stringified values; otherwise print a single stringified value.
        """
        if repeated:
            pref_value = [meshtastic.util.toStr(v) for v in pref_value]
        else:
            pref_value = meshtastic.util.toStr(pref_value)
        print(f"{str(config_type.name)}.{uni_name}: {str(pref_value)}")
        logger.debug(f"{str(config_type.name)}.{uni_name}: {str(pref_value)}")

    name = splitCompoundName(comp_name)
    wholeField = name[0] == name[1]  # We want the whole field

    camel_name = meshtastic.util.snake_to_camel(name[1])
    # Note: protobufs has the keys in snake_case, so snake internally
    snake_name = meshtastic.util.camel_to_snake(name[1])
    uni_name = camel_name if mt_config.camel_case else snake_name
    logger.debug(f"snake_name:{snake_name} camel_name:{camel_name}")
    logger.debug(f"use camel:{mt_config.camel_case}")

    # First validate the input
    localConfig = node.localConfig
    moduleConfig = node.moduleConfig
    found: bool = False
    config = localConfig
    config_type = None
    pref = ""
    for config in [localConfig, moduleConfig]:
        objDesc = config.DESCRIPTOR
        config_type = objDesc.fields_by_name.get(name[0])
        pref = ""  # FIXME - is this correct to leave as an empty string if not found?
        if config_type:
            pref = config_type.message_type.fields_by_name.get(snake_name)
            if pref or wholeField:
                found = True
                break

    if not found:
        print(
            f"{localConfig.__class__.__name__} and {moduleConfig.__class__.__name__} do not have attribute {uni_name}."
        )
        print("Choices are...")
        printConfig(localConfig)
        printConfig(moduleConfig)
        return False

    # Check if we need to request the config
    if config_type is None:
        return False

    if len(config.ListFields()) != 0 and not isinstance(
        pref, str
    ):  # if str, it's still the empty string, I think
        # read the value
        config_values = getattr(config, config_type.name)
        if not wholeField:
            pref_value = getattr(config_values, pref.name)
            repeated = pref.label == pref.LABEL_REPEATED
            _printSetting(config_type, uni_name, pref_value, repeated)
        else:
            for field in config_values.ListFields():
                repeated = field[0].label == field[0].LABEL_REPEATED
                _printSetting(config_type, field[0].name, field[1], repeated)
    else:
        # Always show whole field for remote node
        node.requestConfig(config_type)

    return True


def splitCompoundName(comp_name: str) -> List[str]:
    """
    Split a dot-separated preference name into segments.
    
    If the input contains one or more dots, returns the list of segments produced by splitting on '.'.
    If the input contains no dot, returns a two-element list with the original name repeated.
    
    Parameters:
        comp_name (str): The compound preference name to split.
    
    Returns:
        List[str]: The list of name segments, or a two-element list with the original name duplicated when no dot is present.
    """
    name: List[str] = comp_name.split(".")
    if len(name) < 2:
        name[0] = comp_name
        name.append(comp_name)
    return name


def traverseConfig(config_root, config, interface_config) -> bool:
    """
    Recursively apply configuration values from a nested mapping onto an interface configuration.
    
    Parameters:
        config_root (str): Dot-separated prefix representing the current configuration path (e.g., "channel.0").
        config (dict): Nested mapping of configuration keys to either sub-dictionaries or leaf values to set.
        interface_config: Target configuration object used by setPref to apply leaf values.
    
    Returns:
        bool: `True` when traversal completes (all leaf values were processed).
    """
    snake_name = meshtastic.util.camel_to_snake(config_root)
    for pref in config:
        pref_name = f"{snake_name}.{pref}"
        if isinstance(config[pref], dict):
            traverseConfig(pref_name, config[pref], interface_config)
        else:
            setPref(interface_config, pref_name, config[pref])

    return True


def setPref(config, comp_name, raw_val) -> bool:
    """
    Set a preference or channel field on a protobuf config object identified by a (possibly compound) name.
    
    This resolves a dot-separated comp_name into the appropriate nested protobuf field, converts raw_val to the field's expected type (including resolving enum names), validates certain fields (for example, rejects wifi_psk shorter than 8 characters), and applies the value. Repeated fields are updated by replacing or appending entries (use 0 to clear a repeated field). Field name resolution prefers camelCase or snake_case according to the global camel_case setting.
    
    Parameters:
        config: The protobuf config or channel object to modify.
        comp_name (str): Dot-separated field path (e.g., "channel.security.wifi_psk" or "node.name"); if a single name is given it targets that top-level section.
        raw_val: The value to set; may be a string, number, list (for repeated fields), or an already-typed value. String enum names will be resolved to their numeric values.
    
    Returns:
        bool: `True` if a value was successfully set or updated; `False` if the named field was not found or validation failed.
    """

    name = splitCompoundName(comp_name)

    snake_name = meshtastic.util.camel_to_snake(name[-1])
    camel_name = meshtastic.util.snake_to_camel(name[-1])
    uni_name = camel_name if mt_config.camel_case else snake_name
    logger.debug(f"snake_name:{snake_name}")
    logger.debug(f"camel_name:{camel_name}")

    objDesc = config.DESCRIPTOR
    config_part = config
    config_type = objDesc.fields_by_name.get(name[0])
    if config_type and config_type.message_type is not None:
        for name_part in name[1:-1]:
            part_snake_name = meshtastic.util.camel_to_snake((name_part))
            config_part = getattr(config, config_type.name)
            config_type = config_type.message_type.fields_by_name.get(part_snake_name)
    pref = None
    if config_type and config_type.message_type is not None:
        pref = config_type.message_type.fields_by_name.get(snake_name)
    # Others like ChannelSettings are standalone
    elif config_type:
        pref = config_type

    if (not pref) or (not config_type):
        return False

    if isinstance(raw_val, str):
        val = meshtastic.util.fromStr(raw_val)
    else:
        val = raw_val
    logger.debug(f"valStr:{raw_val} val:{val}")

    if snake_name == "wifi_psk" and len(str(raw_val)) < 8:
        print("Warning: network.wifi_psk must be 8 or more characters.")
        return False

    enumType = pref.enum_type
    if enumType and isinstance(val, str):
        # We've failed so far to convert this string into an enum, try to find it by reflection
        e = enumType.values_by_name.get(val)
        if e:
            val = e.number
        else:
            print(
                f"{name[0]}.{uni_name} does not have an enum called {val}, so you can not set it."
            )
            print("Choices in sorted order are:")
            names = []
            for f in enumType.values:
                # Note: We must use the value of the enum (regardless if camel or snake case)
                names.append(f"{f.name}")
            for temp_name in sorted(names):
                print(f"    {temp_name}")
            return False

    # repeating fields need to be handled with append, not setattr
    if pref.label != pref.LABEL_REPEATED:
        try:
            if config_type.message_type is not None:
                config_values = getattr(config_part, config_type.name)
                setattr(config_values, pref.name, val)
            else:
                setattr(config_part, snake_name, val)
        except TypeError:
            # The setter didn't like our arg type guess try again as a string
            config_values = getattr(config_part, config_type.name)
            setattr(config_values, pref.name, str(val))
    elif isinstance(val, list):
        new_vals = [meshtastic.util.fromStr(x) for x in val]
        config_values = getattr(config, config_type.name)
        getattr(config_values, pref.name)[:] = new_vals
    else:
        config_values = getattr(config, config_type.name)
        if val == 0:
            # clear values
            print(f"Clearing {pref.name} list")
            del getattr(config_values, pref.name)[:]
        else:
            print(f"Adding '{raw_val}' to the {pref.name} list")
            cur_vals = [
                x for x in getattr(config_values, pref.name) if x not in [0, "", b""]
            ]
            if val not in cur_vals:
                cur_vals.append(val)
            getattr(config_values, pref.name)[:] = cur_vals
        return True

    prefix = f"{'.'.join(name[0:-1])}." if config_type.message_type is not None else ""
    print(f"Set {prefix}{uni_name} to {raw_val}")

    return True


def onConnected(interface):
    """Callback invoked when we connect to a radio."""
    closeNow = False  # Should we drop the connection after we finish?
    waitForAckNak = (
        False  # Should we wait for an acknowledgment if we send to a remote node?
    )
    try:
        args = mt_config.args

        # convenient place to store any keyword args we pass to getNode
        getNode_kwargs = {
            "requestChannelAttempts": args.channel_fetch_attempts,
            "timeout": args.timeout,
        }

        # do not print this line if we are exporting the config
        if not args.export_config:
            print("Connected to radio")

        if args.set_time is not None:
            interface.getNode(args.dest, False, **getNode_kwargs).setTime(args.set_time)

        if args.remove_position:
            closeNow = True
            waitForAckNak = True

            print("Removing fixed position and disabling fixed position setting")
            interface.getNode(args.dest, False, **getNode_kwargs).removeFixedPosition()
        elif args.setlat or args.setlon or args.setalt:
            closeNow = True
            waitForAckNak = True

            alt = 0
            lat = 0
            lon = 0
            if args.setalt:
                alt = int(args.setalt)
                print(f"Fixing altitude at {alt} meters")
            if args.setlat:
                try:
                    lat = int(args.setlat)
                except ValueError:
                    lat = float(args.setlat)
                print(f"Fixing latitude at {lat} degrees")
            if args.setlon:
                try:
                    lon = int(args.setlon)
                except ValueError:
                    lon = float(args.setlon)
                print(f"Fixing longitude at {lon} degrees")

            print("Setting device position and enabling fixed position setting")
            # can include lat/long/alt etc: latitude = 37.5, longitude = -122.1
            interface.getNode(args.dest, False, **getNode_kwargs).setFixedPosition(
                lat, lon, alt
            )

        if args.set_owner or args.set_owner_short or args.set_is_unmessageable:
            closeNow = True
            waitForAckNak = True

            long_name = args.set_owner.strip() if args.set_owner else None
            short_name = args.set_owner_short.strip() if args.set_owner_short else None

            if long_name is not None and not long_name:
                meshtastic.util.our_exit(
                    "ERROR: Long Name cannot be empty or contain only whitespace characters"
                )

            if short_name is not None and not short_name:
                meshtastic.util.our_exit(
                    "ERROR: Short Name cannot be empty or contain only whitespace characters"
                )

            if long_name and short_name:
                print(
                    f"Setting device owner to {long_name} and short name to {short_name}"
                )
            elif long_name:
                print(f"Setting device owner to {long_name}")
            elif short_name:
                print(f"Setting device owner short to {short_name}")

            unmessagable = None
            if args.set_is_unmessageable is not None:
                unmessagable = (
                    meshtastic.util.fromStr(args.set_is_unmessageable)
                    if isinstance(args.set_is_unmessageable, str)
                    else args.set_is_unmessageable
                )
                print(f"Setting device owner is_unmessageable to {unmessagable}")

            interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
                long_name=long_name, short_name=short_name, is_unmessagable=unmessagable
            )

        if args.set_canned_message:
            closeNow = True
            waitForAckNak = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            if node.module_available(mesh_pb2.CANNEDMSG_CONFIG):
                print(f"Setting canned plugin message to {args.set_canned_message}")
                node.set_canned_message(args.set_canned_message)
            else:
                print("Canned Message module is excluded by firmware; skipping set.")

        if args.set_ringtone:
            closeNow = True
            waitForAckNak = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            if node.module_available(mesh_pb2.EXTNOTIF_CONFIG):
                print(f"Setting ringtone to {args.set_ringtone}")
                node.set_ringtone(args.set_ringtone)
            else:
                print(
                    "External Notification is excluded by firmware; skipping ringtone set."
                )

        if args.pos_fields:
            # If --pos-fields invoked with args, set position fields
            closeNow = True
            positionConfig = interface.getNode(
                args.dest, **getNode_kwargs
            ).localConfig.position
            allFields = 0

            try:
                for field in args.pos_fields:
                    v_field = positionConfig.PositionFlags.Value(field)
                    allFields |= v_field

            except ValueError:
                print("ERROR: supported position fields are:")
                print(positionConfig.PositionFlags.keys())
                print(
                    "If no fields are specified, will read and display current value."
                )

            else:
                print(f"Setting position fields to {allFields}")
                setPref(positionConfig, "position_flags", f"{allFields:d}")
                print("Writing modified preferences to device")
                interface.getNode(args.dest, **getNode_kwargs).writeConfig("position")

        elif args.pos_fields is not None:
            # If --pos-fields invoked without args, read and display current value
            closeNow = True
            positionConfig = interface.getNode(
                args.dest, **getNode_kwargs
            ).localConfig.position

            fieldNames = []
            for bit in positionConfig.PositionFlags.values():
                if positionConfig.position_flags & bit:
                    fieldNames.append(positionConfig.PositionFlags.Name(bit))
            print(" ".join(fieldNames))

        if args.set_ham:
            if not args.set_ham.strip():
                meshtastic.util.our_exit(
                    "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
                )
            closeNow = True
            print(f"Setting Ham ID to {args.set_ham} and turning off encryption")
            interface.getNode(args.dest, **getNode_kwargs).setOwner(
                args.set_ham, is_licensed=True
            )
            # Must turn off encryption on primary channel
            interface.getNode(
                args.dest, **getNode_kwargs
            ).turnOffEncryptionOnPrimaryChannel()

        if args.reboot:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).reboot()

        if args.reboot_ota:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).rebootOTA()

        if args.enter_dfu:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).enterDFUMode()

        if args.shutdown:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).shutdown()

        if args.device_metadata:
            closeNow = True
            interface.getNode(args.dest, False, **getNode_kwargs).getMetadata()

        if args.begin_edit:
            closeNow = True
            interface.getNode(
                args.dest, False, **getNode_kwargs
            ).beginSettingsTransaction()

        if args.commit_edit:
            closeNow = True
            interface.getNode(
                args.dest, False, **getNode_kwargs
            ).commitSettingsTransaction()

        if args.factory_reset or args.factory_reset_device:
            closeNow = True
            waitForAckNak = True

            full = bool(args.factory_reset_device)
            interface.getNode(args.dest, False, **getNode_kwargs).factoryReset(
                full=full
            )

        if args.remove_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).removeNode(
                args.remove_node
            )

        if args.set_favorite_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).setFavorite(
                args.set_favorite_node
            )

        if args.remove_favorite_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).removeFavorite(
                args.remove_favorite_node
            )

        if args.set_ignored_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).setIgnored(
                args.set_ignored_node
            )

        if args.remove_ignored_node:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).removeIgnored(
                args.remove_ignored_node
            )

        if args.reset_nodedb:
            closeNow = True
            waitForAckNak = True
            interface.getNode(args.dest, False, **getNode_kwargs).resetNodeDb()

        if args.sendtext:
            closeNow = True
            channelIndex = mt_config.channel_index or 0
            if checkChannel(interface, channelIndex):
                print(
                    f"Sending text message {args.sendtext} to {args.dest} on channelIndex:{channelIndex}"
                    f" {'using PRIVATE_APP port' if args.private else ''}"
                )
                interface.sendText(
                    args.sendtext,
                    args.dest,
                    wantAck=True,
                    channelIndex=channelIndex,
                    onResponse=interface.getNode(
                        args.dest, False, **getNode_kwargs
                    ).onAckNak,
                    portNum=(
                        portnums_pb2.PortNum.PRIVATE_APP
                        if args.private
                        else portnums_pb2.PortNum.TEXT_MESSAGE_APP
                    ),
                )
            else:
                meshtastic.util.our_exit(
                    f"Warning: {channelIndex} is not a valid channel. Channel must not be DISABLED."
                )

        if args.traceroute:
            loraConfig = interface.localNode.localConfig.lora
            hopLimit = loraConfig.hop_limit
            dest = str(args.traceroute)
            channelIndex = mt_config.channel_index or 0
            if checkChannel(interface, channelIndex):
                print(
                    f"Sending traceroute request to {dest} on channelIndex:{channelIndex} (this could take a while)"
                )
                interface.sendTraceRoute(dest, hopLimit, channelIndex=channelIndex)

        if args.request_telemetry:
            if args.dest == BROADCAST_ADDR:
                meshtastic.util.our_exit("Warning: Must use a destination node ID.")
            else:
                channelIndex = mt_config.channel_index or 0
                if checkChannel(interface, channelIndex):
                    telemMap = {
                        "device": "device_metrics",
                        "environment": "environment_metrics",
                        "air_quality": "air_quality_metrics",
                        "airquality": "air_quality_metrics",
                        "power": "power_metrics",
                        "localstats": "local_stats",
                        "local_stats": "local_stats",
                    }
                    telemType = telemMap.get(args.request_telemetry, "device_metrics")
                    print(
                        f"Sending {telemType} telemetry request to {args.dest} on channelIndex:{channelIndex} (this could take a while)"
                    )
                    interface.sendTelemetry(
                        destinationId=args.dest,
                        wantResponse=True,
                        channelIndex=channelIndex,
                        telemetryType=telemType,
                    )

        if args.request_position:
            if args.dest == BROADCAST_ADDR:
                meshtastic.util.our_exit("Warning: Must use a destination node ID.")
            else:
                channelIndex = mt_config.channel_index or 0
                if checkChannel(interface, channelIndex):
                    print(
                        f"Sending position request to {args.dest} on channelIndex:{channelIndex} (this could take a while)"
                    )
                    interface.sendPosition(
                        destinationId=args.dest,
                        wantResponse=True,
                        channelIndex=channelIndex,
                    )

        if args.gpio_wrb or args.gpio_rd or args.gpio_watch:
            if args.dest == BROADCAST_ADDR:
                meshtastic.util.our_exit("Warning: Must use a destination node ID.")
            else:
                rhc = remote_hardware.RemoteHardwareClient(interface)

                if args.gpio_wrb:
                    bitmask = 0
                    bitval = 0
                    for wrpair in args.gpio_wrb or []:
                        bitmask |= 1 << int(wrpair[0])
                        bitval |= int(wrpair[1]) << int(wrpair[0])
                    print(
                        f"Writing GPIO mask 0x{bitmask:x} with value 0x{bitval:x} to {args.dest}"
                    )
                    rhc.writeGPIOs(args.dest, bitmask, bitval)
                    closeNow = True

                if args.gpio_rd:
                    bitmask = int(args.gpio_rd, 16)
                    print(f"Reading GPIO mask 0x{bitmask:x} from {args.dest}")
                    interface.mask = bitmask
                    rhc.readGPIOs(args.dest, bitmask, None)
                    # wait up to X seconds for a response
                    for _ in range(10):
                        time.sleep(1)
                        if interface.gotResponse:
                            break
                    logger.debug("end of gpio_rd")

                if args.gpio_watch:
                    bitmask = int(args.gpio_watch, 16)
                    print(
                        f"Watching GPIO mask 0x{bitmask:x} from {args.dest}. Press ctrl-c to exit"
                    )
                    while True:
                        rhc.watchGPIOs(args.dest, bitmask)
                        time.sleep(1)

        # handle settings
        if args.set:
            closeNow = True
            waitForAckNak = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)

            # Handle the int/float/bool arguments
            pref = None
            fields = set()
            for pref in args.set:
                found = False
                field = splitCompoundName(pref[0].lower())[0]
                for config in [node.localConfig, node.moduleConfig]:
                    config_type = config.DESCRIPTOR.fields_by_name.get(field)
                    if config_type:
                        if len(config.ListFields()) == 0:
                            node.requestConfig(
                                config.DESCRIPTOR.fields_by_name.get(field)
                            )
                        found = setPref(config, pref[0], pref[1])
                        if found:
                            fields.add(field)
                            break

            if found:
                print("Writing modified preferences to device")
                if len(fields) > 1:
                    print("Using a configuration transaction")
                    node.beginSettingsTransaction()
                for field in fields:
                    print(f"Writing {field} configuration to device")
                    node.writeConfig(field)
                if len(fields) > 1:
                    node.commitSettingsTransaction()
            else:
                if mt_config.camel_case:
                    print(
                        f"{node.localConfig.__class__.__name__} and {node.moduleConfig.__class__.__name__} do not have an attribute {pref[0]}."
                    )
                else:
                    print(
                        f"{node.localConfig.__class__.__name__} and {node.moduleConfig.__class__.__name__} do not have attribute {pref[0]}."
                    )
                print("Choices are...")
                printConfig(node.localConfig)
                printConfig(node.moduleConfig)

        if args.configure:
            with open(args.configure[0], encoding="utf8") as file:
                configuration = yaml.safe_load(file)
                closeNow = True

                interface.getNode(
                    args.dest, False, **getNode_kwargs
                ).beginSettingsTransaction()

                if "owner" in configuration:
                    # Validate owner name before setting
                    owner_name = str(configuration["owner"]).strip()
                    if not owner_name:
                        meshtastic.util.our_exit(
                            "ERROR: Long Name cannot be empty or contain only whitespace characters"
                        )
                    print(f"Setting device owner to {configuration['owner']}")
                    waitForAckNak = True
                    interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
                        configuration["owner"]
                    )
                    time.sleep(0.5)

                if "owner_short" in configuration:
                    # Validate owner short name before setting
                    owner_short_name = str(configuration["owner_short"]).strip()
                    if not owner_short_name:
                        meshtastic.util.our_exit(
                            "ERROR: Short Name cannot be empty or contain only whitespace characters"
                        )
                    print(
                        f"Setting device owner short to {configuration['owner_short']}"
                    )
                    waitForAckNak = True
                    interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
                        long_name=None, short_name=configuration["owner_short"]
                    )
                    time.sleep(0.5)

                if "ownerShort" in configuration:
                    # Validate owner short name before setting
                    owner_short_name = str(configuration["ownerShort"]).strip()
                    if not owner_short_name:
                        meshtastic.util.our_exit(
                            "ERROR: Short Name cannot be empty or contain only whitespace characters"
                        )
                    print(
                        f"Setting device owner short to {configuration['ownerShort']}"
                    )
                    waitForAckNak = True
                    interface.getNode(args.dest, False, **getNode_kwargs).setOwner(
                        long_name=None, short_name=configuration["ownerShort"]
                    )
                    time.sleep(0.5)

                if "channel_url" in configuration:
                    print("Setting channel url to", configuration["channel_url"])
                    interface.getNode(args.dest, **getNode_kwargs).setURL(
                        configuration["channel_url"]
                    )
                    time.sleep(0.5)

                if "channelUrl" in configuration:
                    print("Setting channel url to", configuration["channelUrl"])
                    interface.getNode(args.dest, **getNode_kwargs).setURL(
                        configuration["channelUrl"]
                    )
                    time.sleep(0.5)

                if "canned_messages" in configuration:
                    print(
                        "Setting canned message messages to",
                        configuration["canned_messages"],
                    )
                    interface.getNode(args.dest, **getNode_kwargs).set_canned_message(
                        configuration["canned_messages"]
                    )
                    time.sleep(0.5)

                if "ringtone" in configuration:
                    print("Setting ringtone to", configuration["ringtone"])
                    interface.getNode(args.dest, **getNode_kwargs).set_ringtone(
                        configuration["ringtone"]
                    )
                    time.sleep(0.5)

                if "location" in configuration:
                    alt = 0
                    lat = 0.0
                    lon = 0.0
                    localConfig = interface.localNode.localConfig

                    if "alt" in configuration["location"]:
                        alt = int(configuration["location"]["alt"] or 0)
                        print(f"Fixing altitude at {alt} meters")
                    if "lat" in configuration["location"]:
                        lat = float(configuration["location"]["lat"] or 0)
                        print(f"Fixing latitude at {lat} degrees")
                    if "lon" in configuration["location"]:
                        lon = float(configuration["location"]["lon"] or 0)
                        print(f"Fixing longitude at {lon} degrees")
                    print("Setting device position")
                    interface.localNode.setFixedPosition(lat, lon, alt)
                    time.sleep(0.5)

                if "config" in configuration:
                    localConfig = interface.getNode(
                        args.dest, **getNode_kwargs
                    ).localConfig
                    for section in configuration["config"]:
                        traverseConfig(
                            section, configuration["config"][section], localConfig
                        )
                        interface.getNode(args.dest, **getNode_kwargs).writeConfig(
                            meshtastic.util.camel_to_snake(section)
                        )
                        time.sleep(0.5)

                if "module_config" in configuration:
                    moduleConfig = interface.getNode(
                        args.dest, **getNode_kwargs
                    ).moduleConfig
                    for section in configuration["module_config"]:
                        traverseConfig(
                            section,
                            configuration["module_config"][section],
                            moduleConfig,
                        )
                        interface.getNode(args.dest, **getNode_kwargs).writeConfig(
                            meshtastic.util.camel_to_snake(section)
                        )
                        time.sleep(0.5)

                interface.getNode(
                    args.dest, False, **getNode_kwargs
                ).commitSettingsTransaction()
                print("Writing modified configuration to device")

        if args.export_config:
            if args.dest != BROADCAST_ADDR:
                print("Exporting configuration of remote nodes is not supported.")
                return

            closeNow = True
            config_txt = export_config(interface)

            if args.export_config == "-":
                # Output to stdout (preserves legacy use of `> file.yaml`)
                print(config_txt)
            else:
                try:
                    with open(args.export_config, "w", encoding="utf-8") as f:
                        f.write(config_txt)
                    print(f"Exported configuration to {args.export_config}")
                except Exception as e:
                    meshtastic.util.our_exit(f"ERROR: Failed to write config file: {e}")

        if args.ch_set_url:
            closeNow = True
            interface.getNode(args.dest, **getNode_kwargs).setURL(
                args.ch_set_url, addOnly=False
            )

        # handle changing channels

        if args.ch_add_url:
            closeNow = True
            interface.getNode(args.dest, **getNode_kwargs).setURL(
                args.ch_add_url, addOnly=True
            )

        if args.ch_add:
            channelIndex = mt_config.channel_index
            if channelIndex is not None:
                # Since we set the channel index after adding a channel, don't allow --ch-index
                meshtastic.util.our_exit(
                    "Warning: '--ch-add' and '--ch-index' are incompatible. Channel not added."
                )
            closeNow = True
            if len(args.ch_add) > 10:
                meshtastic.util.our_exit(
                    "Warning: Channel name must be shorter. Channel not added."
                )
            n = interface.getNode(args.dest, **getNode_kwargs)
            ch = n.getChannelByName(args.ch_add)
            if ch:
                meshtastic.util.our_exit(
                    f"Warning: This node already has a '{args.ch_add}' channel. No changes were made."
                )
            else:
                # get the first channel that is disabled (i.e., available)
                ch = n.getDisabledChannel()
                if not ch:
                    meshtastic.util.our_exit("Warning: No free channels were found")
                chs = channel_pb2.ChannelSettings()
                chs.psk = meshtastic.util.genPSK256()
                chs.name = args.ch_add
                ch.settings.CopyFrom(chs)
                ch.role = channel_pb2.Channel.Role.SECONDARY
                print("Writing modified channels to device")
                n.writeChannel(ch.index)
                if channelIndex is None:
                    print(
                        f"Setting newly-added channel's {ch.index} as '--ch-index' for further modifications"
                    )
                    mt_config.channel_index = ch.index

        if args.ch_del:
            closeNow = True

            channelIndex = mt_config.channel_index
            if channelIndex is None:
                meshtastic.util.our_exit(
                    "Warning: Need to specify '--ch-index' for '--ch-del'.", 1
                )
            else:
                if channelIndex == 0:
                    meshtastic.util.our_exit(
                        "Warning: Cannot delete primary channel.", 1
                    )
                else:
                    print(f"Deleting channel {channelIndex}")
                    ch = interface.getNode(args.dest, **getNode_kwargs).deleteChannel(
                        channelIndex
                    )

        def setSimpleConfig(modem_preset):
            """
            Set the modem preset for the device's primary channel and persist the change.
            
            If a non-primary channel is selected, the function exits with a warning. The function ensures the node's local configuration is loaded, updates the LORA `modem_preset` field to the provided value, and writes the `lora` section back to the device.
            
            Parameters:
                modem_preset: The modem preset identifier to apply (numeric or enum value expected).
            """
            channelIndex = mt_config.channel_index
            if channelIndex is not None and channelIndex > 0:
                meshtastic.util.our_exit(
                    "Warning: Cannot set modem preset for non-primary channel", 1
                )
            # Overwrite modem_preset
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            if len(node.localConfig.ListFields()) == 0:
                node.requestConfig(
                    node.localConfig.DESCRIPTOR.fields_by_name.get("lora")
                )
            node.localConfig.lora.modem_preset = modem_preset
            node.writeConfig("lora")

        # handle the simple radio set commands
        if args.ch_vlongslow:
            setSimpleConfig(config_pb2.Config.LoRaConfig.ModemPreset.VERY_LONG_SLOW)

        if args.ch_longslow:
            setSimpleConfig(config_pb2.Config.LoRaConfig.ModemPreset.LONG_SLOW)

        if args.ch_longfast:
            setSimpleConfig(config_pb2.Config.LoRaConfig.ModemPreset.LONG_FAST)

        if args.ch_medslow:
            setSimpleConfig(config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_SLOW)

        if args.ch_medfast:
            setSimpleConfig(config_pb2.Config.LoRaConfig.ModemPreset.MEDIUM_FAST)

        if args.ch_shortslow:
            setSimpleConfig(config_pb2.Config.LoRaConfig.ModemPreset.SHORT_SLOW)

        if args.ch_shortfast:
            setSimpleConfig(config_pb2.Config.LoRaConfig.ModemPreset.SHORT_FAST)

        if args.ch_set or args.ch_enable or args.ch_disable:
            closeNow = True

            channelIndex = mt_config.channel_index
            if channelIndex is None:
                meshtastic.util.our_exit("Warning: Need to specify '--ch-index'.", 1)
            node = interface.getNode(args.dest, **getNode_kwargs)
            ch = node.channels[channelIndex]

            if args.ch_enable or args.ch_disable:
                print(
                    "Warning: --ch-enable and --ch-disable can produce noncontiguous channels, "
                    "which can cause errors in some clients. Whenever possible, use --ch-add and --ch-del instead."
                )
                if channelIndex == 0:
                    meshtastic.util.our_exit(
                        "Warning: Cannot enable/disable PRIMARY channel."
                    )

                enable = True  # default to enable
                if args.ch_enable:
                    enable = True
                if args.ch_disable:
                    enable = False

            # Handle the channel settings
            for pref in args.ch_set or []:
                if pref[0] == "psk":
                    found = True
                    ch.settings.psk = meshtastic.util.fromPSK(pref[1])
                else:
                    found = setPref(ch.settings, pref[0], pref[1])
                if not found:
                    category_settings = ["module_settings"]
                    print(
                        f"{ch.settings.__class__.__name__} does not have an attribute {pref[0]}."
                    )
                    print("Choices are...")
                    for field in ch.settings.DESCRIPTOR.fields:
                        if field.name not in category_settings:
                            print(f"{field.name}")
                        else:
                            print(f"{field.name}:")
                            config = ch.settings.DESCRIPTOR.fields_by_name.get(
                                field.name
                            )
                            names = []
                            for sub_field in config.message_type.fields:
                                tmp_name = f"{field.name}.{sub_field.name}"
                                names.append(tmp_name)
                            for temp_name in sorted(names):
                                print(f"    {temp_name}")

                enable = True  # If we set any pref, assume the user wants to enable the channel

            if enable:
                ch.role = (
                    channel_pb2.Channel.Role.PRIMARY
                    if (channelIndex == 0)
                    else channel_pb2.Channel.Role.SECONDARY
                )
            else:
                ch.role = channel_pb2.Channel.Role.DISABLED

            print("Writing modified channels to device")
            node.writeChannel(channelIndex)

        if args.get_canned_message:
            closeNow = True
            print("")
            messages = interface.getNode(
                args.dest, **getNode_kwargs
            ).get_canned_message()
            print(f"canned_plugin_message:{messages}")

        if args.get_ringtone:
            closeNow = True
            print("")
            ringtone = interface.getNode(args.dest, **getNode_kwargs).get_ringtone()
            print(f"ringtone:{ringtone}")

        if args.info:
            print("")
            # If we aren't trying to talk to our local node, don't show it
            if args.dest == BROADCAST_ADDR:
                interface.showInfo()
                print("")
                interface.getNode(args.dest, **getNode_kwargs).showInfo()
                closeNow = True
                print("")
                pypi_version = meshtastic.util.check_if_newer_version()
                if pypi_version:
                    print(
                        f"*** A newer version v{pypi_version} is available!"
                        ' Consider running "pip install --upgrade meshtastic" ***\n'
                    )
            else:
                print("Showing info of remote node is not supported.")
                print(
                    "Use the '--get' command for a specific configuration (e.g. 'lora') instead."
                )

        if args.get:
            closeNow = True
            node = interface.getNode(args.dest, False, **getNode_kwargs)
            found = False
            for pref in args.get:
                found = getPref(node, pref[0])

            if found:
                print("Completed getting preferences")

        if args.nodes:
            closeNow = True
            if args.dest != BROADCAST_ADDR:
                print("Showing node list of a remote node is not supported.")
                return
            interface.showNodes(True, args.show_fields)

        if args.show_fields and not args.nodes:
            print("--show-fields can only be used with --nodes")
            return

        if args.qr or args.qr_all:
            closeNow = True
            url = interface.getNode(args.dest, True, **getNode_kwargs).getURL(
                includeAll=args.qr_all
            )
            if args.qr_all:
                urldesc = "Complete URL (includes all channels)"
            else:
                urldesc = "Primary channel URL"
            print(f"{urldesc}: {url}")
            if pyqrcode is not None:
                qr = pyqrcode.create(url)
                print(qr.terminal())
            else:
                print("Install pyqrcode to view a QR code printed to terminal.")

        log_set: Any = None
        # we need to keep a reference to the logset so it doesn't get GCed early

        if args.slog or args.power_stress:
            if have_powermon:
                # Setup loggers
                global meter  # pylint: disable=global-variable-not-assigned
                log_set = LogSet(
                    interface, args.slog if args.slog != "default" else None, meter
                )

                if args.power_stress:
                    stress = PowerStress(interface)
                    stress.run()
                    closeNow = True  # exit immediately after stress test
            else:
                meshtastic.util.our_exit(
                    "The powermon module could not be loaded. "
                    "You may need to run `poetry install --with powermon`. "
                    f"Import Error was: {powermon_exception}"
                )

        if args.listen:
            closeNow = False

        have_tunnel = platform.system() == "Linux"
        if have_tunnel and args.tunnel:
            if args.dest != BROADCAST_ADDR:
                print("A tunnel can only be created using the local node.")
                return
            # Even if others said we could close, stay open if the user asked for a tunnel
            closeNow = False
            if interface.noProto:
                logger.warning("Not starting Tunnel - disabled by noProto")
            else:
                from . import tunnel  # pylint: disable=C0415

                if args.tunnel_net:
                    tunnel.Tunnel(interface, subnet=args.tunnel_net)
                else:
                    tunnel.Tunnel(interface)

        if args.ack or (args.dest != BROADCAST_ADDR and waitForAckNak):
            print(
                "Waiting for an acknowledgment from remote node (this could take a while)"
            )
            interface.getNode(args.dest, False, **getNode_kwargs).iface.waitForAckNak()

        if args.wait_to_disconnect:
            print(f"Waiting {args.wait_to_disconnect} seconds before disconnecting")
            time.sleep(int(args.wait_to_disconnect))

        # if the user didn't ask for serial debugging output, we might want to exit after we've done our operation
        if (not args.seriallog) and closeNow:
            interface.close()  # after running command then exit

        # Close any structured logs after we've done all of our API operations
        if log_set:
            log_set.close()

    except Exception as ex:
        print(f"Aborting due to: {ex}")
        interface.close()  # close the connection now, so that our app exits
        sys.exit(1)


def printConfig(config) -> None:
    """
    Prints the top-level configuration sections and their field names.
    
    Parameters:
        config: A protobuf-like configuration message (must provide a DESCRIPTOR). The function skips the "version" section and prints each other section name followed by its fields in sorted order as "section.field". If mt_config.camel_case is true, field names are converted to camelCase before printing. Output is written to standard output.
    """
    objDesc = config.DESCRIPTOR
    for config_section in objDesc.fields:
        if config_section.name != "version":
            config = objDesc.fields_by_name.get(config_section.name)
            print(f"{config_section.name}:")
            names = []
            for field in config.message_type.fields:
                tmp_name = f"{config_section.name}.{field.name}"
                if mt_config.camel_case:
                    tmp_name = meshtastic.util.snake_to_camel(tmp_name)
                names.append(tmp_name)
            for temp_name in sorted(names):
                print(f"    {temp_name}")


def onNode(node) -> None:
    """Callback invoked when the node DB changes."""
    print(f"Node changed: {node}")


def subscribe() -> None:
    """
    Register the default pub-sub handlers needed to receive incoming mesh messages.
    
    Subscribes the local receive callback to the "meshtastic.receive" topic so incoming packets are delivered to the onReceive handler. Other topic subscriptions are intentionally left commented out.
    """
    pub.subscribe(onReceive, "meshtastic.receive")
    # pub.subscribe(onConnection, "meshtastic.connection")

    # We now call onConnected from main
    # pub.subscribe(onConnected, "meshtastic.connection.established")

    # pub.subscribe(onNode, "meshtastic.node")


def set_missing_flags_false(
    config_dict: Dict[str, Any], true_defaults: Set[Tuple[str, str]]
) -> None:
    """
    Ensure specified boolean flags exist in a nested config dictionary and set any missing ones to False.
    
    Parameters:
        config_dict (Dict[str, Any]): The nested configuration dictionary to modify in place.
        true_defaults (Set[Tuple[str, str]]): A set of key paths (tuples of keys) where the final key is expected to be a boolean defaulted to True; any path not present in config_dict will have its final key created and set to False.
    """
    for path in true_defaults:
        d = config_dict
        for key in path[:-1]:
            if key not in d or not isinstance(d[key], dict):
                d[key] = {}
            d = d[key]
        if path[-1] not in d:
            d[path[-1]] = False


def export_config(interface: meshtastic.mesh_interface.MeshInterface) -> str:
    """
    Export the local node and module configuration as a YAML-formatted Meshtastic configuration string.
    
    Produces a YAML document containing selected top-level metadata (owner, owner_short, channel URL, canned messages, ringtone, and location) plus `config` and `module_config` sections derived from the node's protobuf-backed settings. Key casing in the exported `config` and `module_config` follows mt_config.camel_case. Certain boolean flags are explicitly set to false if missing, and security key fields are normalized to include a "base64:" prefix when appropriate.
    
    Parameters:
        interface (MeshInterface): The connected interface whose local node and module configuration will be exported.
    
    Returns:
        str: A YAML string (prefixed with a header comment) representing the exported configuration.
    """
    configObj = {}

    # A list of configuration keys that should be set to False if they are missing
    config_true_defaults = {
        ("bluetooth", "enabled"),
        ("lora", "sx126xRxBoostedGain"),
        ("lora", "txEnabled"),
        ("lora", "usePreset"),
        ("position", "positionBroadcastSmartEnabled"),
        ("security", "serialEnabled"),
    }

    module_true_defaults = {
        ("mqtt", "encryptionEnabled"),
    }

    owner = interface.getLongName()
    owner_short = interface.getShortName()
    channel_url = interface.localNode.getURL()
    myinfo = interface.getMyNodeInfo()
    canned_messages = interface.getCannedMessage()
    ringtone = interface.getRingtone()
    pos = myinfo.get("position")
    lat = None
    lon = None
    alt = None
    if pos:
        lat = pos.get("latitude")
        lon = pos.get("longitude")
        alt = pos.get("altitude")

    if owner:
        configObj["owner"] = owner
    if owner_short:
        configObj["owner_short"] = owner_short
    if channel_url:
        if mt_config.camel_case:
            configObj["channelUrl"] = channel_url
        else:
            configObj["channel_url"] = channel_url
    if canned_messages:
        configObj["canned_messages"] = canned_messages
    if ringtone:
        configObj["ringtone"] = ringtone
    # lat and lon don't make much sense without the other (so fill with 0s), and alt isn't meaningful without both
    if lat or lon:
        configObj["location"] = {"lat": lat or float(0), "lon": lon or float(0)}
        if alt:
            configObj["location"]["alt"] = alt

    config = MessageToDict(interface.localNode.localConfig)
    if config:
        # Ensure explicit false values are present before key conversion.
        set_missing_flags_false(config, config_true_defaults)

        # Convert inner keys to correct snake/camelCase.
        prefs = {}
        for pref, value in config.items():
            pref_key = (
                meshtastic.util.snake_to_camel(pref)
                if mt_config.camel_case
                else meshtastic.util.camel_to_snake(pref)
            )
            prefs[pref_key] = value
            # mark base64 encoded fields as such
            if pref == "security" and isinstance(prefs[pref_key], dict):
                security = prefs[pref_key]
                # Normalize keys to canonical camelCase for reliable lookup,
                # since MessageToDict may produce inconsistent casing
                normalized_key_map = {
                    meshtastic.util.snake_to_camel(
                        meshtastic.util.camel_to_snake(key)
                    ): key
                    for key in security
                    if isinstance(key, str)
                }

                private_key = normalized_key_map.get("privateKey")
                if private_key and isinstance(security.get(private_key), str):
                    security[private_key] = "base64:" + security[private_key]

                public_key = normalized_key_map.get("publicKey")
                if public_key and isinstance(security.get(public_key), str):
                    security[public_key] = "base64:" + security[public_key]

                admin_key = normalized_key_map.get("adminKey")
                admin_keys = security.get(admin_key) if admin_key else None
                if isinstance(admin_keys, list):
                    security[admin_key] = [
                        "base64:" + key if isinstance(key, str) else key
                        for key in admin_keys
                    ]
        configObj["config"] = prefs

    module_config = MessageToDict(interface.localNode.moduleConfig)
    if module_config:
        # Ensure explicit false values are present before key conversion.
        set_missing_flags_false(module_config, module_true_defaults)

        # Convert inner keys to correct snake/camelCase.
        prefs = {}
        for pref, value in module_config.items():
            pref_key = (
                meshtastic.util.snake_to_camel(pref)
                if mt_config.camel_case
                else meshtastic.util.camel_to_snake(pref)
            )
            prefs[pref_key] = value
        configObj["module_config"] = prefs

    config_txt = "# start of Meshtastic configure yaml\n"
    # was used as a string here and a Dictionary above
    config_txt += yaml.dump(configObj)
    return config_txt


def create_power_meter():
    """Setup the power meter."""

    global meter  # pylint: disable=global-statement
    args = mt_config.args

    # If the user specified a voltage, make sure it is valid
    v = 0.0
    if args.power_voltage:
        v = float(args.power_voltage)
        if v < 0.8 or v > 5.0:
            meshtastic.util.our_exit("Voltage must be between 0.8 and 5.0")

    if args.power_riden:
        meter = RidenPowerSupply(args.power_riden)
    elif args.power_ppk2_supply or args.power_ppk2_meter:
        meter = PPK2PowerSupply()
        assert v > 0, "Voltage must be specified for PPK2"
        meter.v = v  # PPK2 requires setting voltage before selecting supply mode
        meter.setIsSupply(args.power_ppk2_supply)
    elif args.power_sim:
        meter = SimPowerSupply()

    if meter and v:
        logger.info(f"Setting power supply to {v} volts")
        meter.v = v
        meter.powerOn()

        if args.power_wait:
            input("Powered on, press enter to continue...")
        else:
            logger.info("Powered-on, waiting for device to boot")
            time.sleep(5)


def common():
    """
    Run shared CLI setup, validate arguments, establish logging and the transport interface, then invoke onConnected and optionally enter the main event loop.
    
    Performs early argument validation and actions (support/test handling, owner/name validations, optional power meter creation, channel/destination defaults), configures logging and serial logging output, subscribes to topics, handles BLE scan/connection, TCP host parsing/connection (including bracketed IPv6 and host:port forms), or serial connection (with user-friendly error messages), and constructs the appropriate Mesh interface. After creating the interface it calls onConnected(client). If the invocation requests a persistent session (listen, tunnel, noproto, or reply modes) this function enters a blocking wait loop until interrupted. On fatal errors it may terminate the process by calling meshtastic.util.our_exit.
    """
    logfile = None
    args = mt_config.args
    parser = mt_config.parser
    logging.basicConfig(
        level=logging.DEBUG if (args.debug or args.listen) else logging.INFO,
        format="%(levelname)s file:%(filename)s %(funcName)s line:%(lineno)s %(message)s",
    )

    # set all meshtastic loggers to DEBUG
    if not (args.debug or args.listen) and args.debuglib:
        logging.getLogger("meshtastic").setLevel(logging.DEBUG)

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        meshtastic.util.our_exit("", 1)
    else:
        if args.support:
            meshtastic.util.support_info()
            meshtastic.util.our_exit("", 0)

        # Early validation for owner names before attempting device connection
        if args.set_owner is not None:
            stripped_long_name = args.set_owner.strip()
            if not stripped_long_name:
                meshtastic.util.our_exit(
                    "ERROR: Long Name cannot be empty or contain only whitespace characters"
                )

        if args.set_owner_short is not None:
            stripped_short_name = args.set_owner_short.strip()
            if not stripped_short_name:
                meshtastic.util.our_exit(
                    "ERROR: Short Name cannot be empty or contain only whitespace characters"
                )

        if args.set_ham is not None:
            stripped_ham_name = args.set_ham.strip()
            if not stripped_ham_name:
                meshtastic.util.our_exit(
                    "ERROR: Ham radio callsign cannot be empty or contain only whitespace characters"
                )

        if have_powermon:
            create_power_meter()

        if args.ch_index is not None:
            channelIndex = int(args.ch_index)
            mt_config.channel_index = channelIndex

        if not args.dest:
            args.dest = BROADCAST_ADDR

        if not args.seriallog:
            if args.noproto:
                args.seriallog = "stdout"
            else:
                args.seriallog = "none"  # assume no debug output in this case

        if args.deprecated is not None:
            logger.error(
                "This option has been deprecated, see help below for the correct replacement..."
            )
            parser.print_help(sys.stderr)
            meshtastic.util.our_exit("", 1)
        elif args.test:
            if meshtastic_test is None:
                meshtastic.util.our_exit(
                    "Test module could not be imported. Ensure you have the 'dotmap' module installed."
                )
            else:
                result = meshtastic_test.testAll()
                if not result:
                    meshtastic.util.our_exit("Warning: Test was not successful.")
                else:
                    meshtastic.util.our_exit("Test was a success.", 0)
        else:
            if args.seriallog == "stdout":
                logfile = sys.stdout
            elif args.seriallog == "none":
                args.seriallog = None
                logger.debug("Not logging serial output")
                logfile = None
            else:
                logger.info(f"Logging serial output to {args.seriallog}")
                # Note: using "line buffering"
                # pylint: disable=R1732
                logfile = open(args.seriallog, "w+", buffering=1, encoding="utf8")
                mt_config.logfile = logfile

            subscribe()
            if args.ble_scan:
                logger.debug("BLE scan starting")
                for x in BLEInterface.scan():
                    print(f"Found: name='{x.name}' address='{x.address}'")
                meshtastic.util.our_exit("BLE scan finished", 0)
            elif args.ble:
                client = BLEInterface(
                    args.ble if args.ble != "any" else None,
                    debugOut=logfile,
                    noProto=args.noproto,
                    noNodes=args.no_nodes,
                    timeout=args.timeout,
                    auto_reconnect=args.ble_auto_reconnect,
                )
            elif args.host:
                try:
                    if args.host.startswith("["):
                        # Bracketed IPv6: [addr] or [addr]:port
                        bracket_end = args.host.find("]")
                        if bracket_end == -1:
                            meshtastic.util.our_exit(
                                f"Error: malformed IPv6 address in --host '{args.host}'.",
                                1,
                            )
                        tcp_hostname = args.host[1:bracket_end]
                        remainder = args.host[bracket_end + 1 :]
                        if remainder.startswith(":"):
                            tcp_port_str = remainder[1:]
                            try:
                                tcp_port = int(tcp_port_str)
                                if not 1 <= tcp_port <= 65535:
                                    raise ValueError(f"Port {tcp_port} out of range")
                            except ValueError:
                                meshtastic.util.our_exit(
                                    f"Error: invalid TCP port in --host '{args.host}'.",
                                    1,
                                )
                        elif remainder:
                            meshtastic.util.our_exit(
                                f"Error: unexpected characters after IPv6 address in --host '{args.host}'.",
                                1,
                            )
                        else:
                            tcp_port = meshtastic.tcp_interface.DEFAULT_TCP_PORT
                    elif ":" in args.host:
                        # Multiple colons → almost certainly an IPv6 address, not host:port
                        if args.host.count(":") > 1:
                            tcp_hostname = args.host
                            tcp_port = meshtastic.tcp_interface.DEFAULT_TCP_PORT
                        else:
                            # Exactly one colon → host:port
                            tcp_hostname, tcp_port_str = args.host.rsplit(":", 1)
                            try:
                                tcp_port = int(tcp_port_str)
                                if not 1 <= tcp_port <= 65535:
                                    raise ValueError(f"Port {tcp_port} out of range")
                            except ValueError:
                                # Not a valid port – treat the whole string as a hostname
                                tcp_hostname = args.host
                                tcp_port = meshtastic.tcp_interface.DEFAULT_TCP_PORT
                    else:
                        tcp_hostname = args.host
                        tcp_port = meshtastic.tcp_interface.DEFAULT_TCP_PORT
                    client = meshtastic.tcp_interface.TCPInterface(
                        tcp_hostname,
                        portNumber=tcp_port,
                        debugOut=logfile,
                        noProto=args.noproto,
                        noNodes=args.no_nodes,
                        timeout=args.timeout,
                    )
                except Exception as ex:
                    meshtastic.util.our_exit(f"Error connecting to {args.host}:{ex}", 1)
            else:
                try:
                    client = meshtastic.serial_interface.SerialInterface(
                        args.port,
                        debugOut=logfile,
                        noProto=args.noproto,
                        noNodes=args.no_nodes,
                        timeout=args.timeout,
                    )
                except FileNotFoundError:
                    # Handle the case where the serial device is not found
                    message = "File Not Found Error:\n"
                    message += f"  The serial device at '{args.port}' was not found.\n"
                    message += "  Please check the following:\n"
                    message += "    1. Is the device connected properly?\n"
                    message += "    2. Is the correct serial port specified?\n"
                    message += "    3. Are the necessary drivers installed?\n"
                    message += "    4. Are you using a **power-only USB cable**? A power-only cable cannot transmit data.\n"
                    message += (
                        "       Ensure you are using a **data-capable USB cable**.\n"
                    )
                    meshtastic.util.our_exit(message, 1)
                except PermissionError as ex:
                    username = os.getlogin()
                    message = "Permission Error:\n"
                    message += (
                        "  Need to add yourself to the 'dialout' group by running:\n"
                    )
                    message += f"     sudo usermod -a -G dialout {username}\n"
                    message += "  After running that command, log out and re-login for it to take effect.\n"
                    message += f"Error was:{ex}"
                    meshtastic.util.our_exit(message)
                except OSError as ex:
                    message = "OS Error:\n"
                    message += "  The serial device couldn't be opened, it might be in use by another process.\n"
                    message += "  Please close any applications or webpages that may be using the device and try again.\n"
                    message += f"\nOriginal error: {ex}"
                    meshtastic.util.our_exit(message)
                if client.devPath is None:
                    try:
                        client = meshtastic.tcp_interface.TCPInterface(
                            "localhost",
                            debugOut=logfile,
                            noProto=args.noproto,
                            noNodes=args.no_nodes,
                            timeout=args.timeout,
                        )
                    except Exception as ex:
                        meshtastic.util.our_exit(
                            f"Error connecting to localhost:{ex}", 1
                        )

            # We assume client is fully connected now
            onConnected(client)

            have_tunnel = platform.system() == "Linux"
            if (
                args.noproto
                or args.reply
                or (have_tunnel and args.tunnel)
                or args.listen
            ):  # loop until someone presses ctrlc
                try:
                    while True:
                        time.sleep(1000)
                except KeyboardInterrupt:
                    logger.info("Exiting due to keyboard interrupt")

        # don't call exit, background threads might be running still
        # sys.exit(0)


def addConnectionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add connection specification arguments."""

    outer = parser.add_argument_group(
        "Connection",
        "Optional arguments that specify how to connect to a Meshtastic device.",
    )
    group = outer.add_mutually_exclusive_group()
    group.add_argument(
        "--port",
        "--serial",
        "-s",
        help="The port of the device to connect to using serial, e.g. /dev/ttyUSB0. (defaults to trying to detect a port)",
        nargs="?",
        const=None,
        default=None,
    )

    group.add_argument(
        "--host",
        "--tcp",
        "-t",
        help="Connect to a device using TCP, optionally passing hostname or IP address to use. (defaults to '%(const)s')",
        nargs="?",
        default=None,
        const="localhost",
    )

    group.add_argument(
        "--ble",
        "-b",
        help="Connect to a BLE device, optionally specifying a device name (defaults to '%(const)s')",
        nargs="?",
        default=None,
        const="any",
    )

    outer.add_argument(
        "--ble-scan",
        help="Scan for Meshtastic BLE devices that may be available to connect to",
        action="store_true",
    )

    outer.add_argument(
        "--ble-auto-reconnect",
        help="Enable BLE auto-reconnect after unexpected disconnects (disabled by default)",
        action="store_true",
    )

    return parser


def addSelectionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Add CLI arguments for selecting a destination node and a channel index.
    
    Adds:
    - `--dest`: Specify the destination node (node ID with '!' or '0x' prefix, or node number). If omitted, '^all' or '^local' is assumed by callers.
    - `--ch-index`: Specify the channel index to target (0 is the PRIMARY channel).
    
    Parameters:
        parser (argparse.ArgumentParser): The argument parser to extend.
    
    Returns:
        argparse.ArgumentParser: The same parser instance with the selection arguments added.
    """
    group = parser.add_argument_group(
        "Selection", "Arguments that select channels to use, destination nodes, etc."
    )

    group.add_argument(
        "--dest",
        help="The destination node id for any sent commands. If not set '^all' or '^local' is assumed."
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        default=None,
        metavar="!xxxxxxxx",
    )

    group.add_argument(
        "--ch-index",
        help="Set the specified channel index for channel-specific commands. Channels start at 0 (0 is the PRIMARY channel).",
        action="store",
        metavar="INDEX",
    )

    return parser


def addImportExportArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add import/export config arguments."""
    group = parser.add_argument_group(
        "Import/Export",
        "Arguments that concern importing and exporting configuration of Meshtastic devices",
    )

    group.add_argument(
        "--configure",
        help="Specify a path to a yaml(.yml) file containing the desired settings for the connected device.",
        action="append",
    )
    group.add_argument(
        "--export-config",
        nargs="?",
        const="-",  # default to "-" if no value provided
        metavar="FILE",
        help="Export device config as YAML (to stdout if no file given)",
    )
    return parser


def addConfigArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register CLI arguments related to device configuration on the given ArgumentParser.
    
    Adds arguments for getting and setting preference fields, beginning/committing
    settings transactions, canned messages and ringtones, modem preset shortcuts,
    owner/ham settings, unmessageable flag, and channel URL helpers.
    
    Parameters:
        parser (argparse.ArgumentParser): The argument parser to extend.
    
    Returns:
        argparse.ArgumentParser: The same parser instance with configuration arguments added.
    """

    group = parser.add_argument_group(
        "Configuration",
        "Arguments that concern general configuration of Meshtastic devices",
    )

    group.add_argument(
        "--get",
        help=(
            "Get a preferences field. Use an invalid field such as '0' to get a list of all fields."
            " Can use either snake_case or camelCase format. (ex: 'power.ls_secs' or 'power.lsSecs')"
        ),
        nargs=1,
        action="append",
        metavar="FIELD",
    )

    group.add_argument(
        "--set",
        help=(
            "Set a preferences field. Can use either snake_case or camelCase format."
            " (ex: 'power.ls_secs' or 'power.lsSecs'). May be less reliable when"
            " setting properties from more than one configuration section."
        ),
        nargs=2,
        action="append",
        metavar=("FIELD", "VALUE"),
    )

    group.add_argument(
        "--begin-edit",
        help="Tell the node to open a transaction to edit settings",
        action="store_true",
    )

    group.add_argument(
        "--commit-edit",
        help="Tell the node to commit open settings transaction",
        action="store_true",
    )

    group.add_argument(
        "--get-canned-message",
        help="Show the canned message plugin message",
        action="store_true",
    )

    group.add_argument(
        "--set-canned-message",
        help="Set the canned messages plugin message (up to 200 characters).",
        action="store",
    )

    group.add_argument(
        "--get-ringtone", help="Show the stored ringtone", action="store_true"
    )

    group.add_argument(
        "--set-ringtone",
        help="Set the Notification Ringtone (up to 230 characters).",
        action="store",
        metavar="RINGTONE",
    )

    group.add_argument(
        "--ch-vlongslow",
        help="Change to the very long-range and slow modem preset",
        action="store_true",
    )

    group.add_argument(
        "--ch-longslow",
        help="Change to the long-range and slow modem preset",
        action="store_true",
    )

    group.add_argument(
        "--ch-longfast",
        help="Change to the long-range and fast modem preset",
        action="store_true",
    )

    group.add_argument(
        "--ch-medslow",
        help="Change to the med-range and slow modem preset",
        action="store_true",
    )

    group.add_argument(
        "--ch-medfast",
        help="Change to the med-range and fast modem preset",
        action="store_true",
    )

    group.add_argument(
        "--ch-shortslow",
        help="Change to the short-range and slow modem preset",
        action="store_true",
    )

    group.add_argument(
        "--ch-shortfast",
        help="Change to the short-range and fast modem preset",
        action="store_true",
    )

    group.add_argument("--set-owner", help="Set device owner name", action="store")

    group.add_argument(
        "--set-owner-short", help="Set device owner short name", action="store"
    )

    group.add_argument(
        "--set-ham", help="Set licensed Ham ID and turn off encryption", action="store"
    )

    group.add_argument(
        "--set-is-unmessageable",
        "--set-is-unmessagable",
        help="Set if a node is messageable or not",
        action="store",
    )

    group.add_argument(
        "--ch-set-url",
        "--seturl",
        help="Set all channels and set LoRa config from a supplied URL",
        metavar="URL",
        action="store",
    )

    group.add_argument(
        "--ch-add-url",
        help="Add secondary channels and set LoRa config from a supplied URL",
        metavar="URL",
        default=None,
    )

    return parser


def addChannelConfigArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register channel-related CLI options on the given argument parser.
    
    Parameters:
        parser (argparse.ArgumentParser): The parser to extend with channel configuration arguments.
    
    Returns:
        argparse.ArgumentParser: The same parser instance with channel configuration options added.
    """

    group = parser.add_argument_group(
        "Channel Configuration",
        "Arguments that concern configuration of channels",
    )

    group.add_argument(
        "--ch-add",
        help="Add a secondary channel, you must specify a channel name",
        default=None,
    )

    group.add_argument(
        "--ch-del", help="Delete the ch-index channel", action="store_true"
    )

    group.add_argument(
        "--ch-set",
        help=(
            "Set a channel parameter. To see channel settings available:'--ch-set all all --ch-index 0'. "
            "Can set the 'psk' using this command. To disable encryption on primary channel:'--ch-set psk none --ch-index 0'. "
            "To set encryption with a new random key on second channel:'--ch-set psk random --ch-index 1'. "
            "To set encryption back to the default:'--ch-set psk default --ch-index 0'. To set encryption with your "
            "own key: '--ch-set psk 0x1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b --ch-index 0'."
        ),
        nargs=2,
        action="append",
        metavar=("FIELD", "VALUE"),
    )

    group.add_argument(
        "--channel-fetch-attempts",
        help=(
            "Attempt to retrieve channel settings for --ch-set this many times before giving up. Default %(default)s."
        ),
        default=3,
        type=int,
        metavar="ATTEMPTS",
    )

    group.add_argument(
        "--qr",
        help=(
            "Display a QR code for the node's primary channel (or all channels with --qr-all). "
            "Also shows the shareable channel URL."
        ),
        action="store_true",
    )

    group.add_argument(
        "--qr-all",
        help="Display a QR code and URL for all of the node's channels.",
        action="store_true",
    )

    group.add_argument(
        "--ch-enable",
        help="Enable the specified channel. Use --ch-add instead whenever possible.",
        action="store_true",
        dest="ch_enable",
        default=False,
    )

    # Note: We are doing a double negative here (Do we want to disable? If ch_disable==True, then disable.)
    group.add_argument(
        "--ch-disable",
        help="Disable the specified channel Use --ch-del instead whenever possible.",
        action="store_true",
        dest="ch_disable",
        default=False,
    )

    return parser


def addPositionConfigArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register CLI arguments for fixed-position and position-related configuration.
    
    Adds --setalt, --setlat, --setlon to set a fixed position (enables fixed position), --remove-position to clear it, and --pos-fields to specify which position fields to send.
    
    Parameters:
        parser (argparse.ArgumentParser): The argument parser to extend.
    
    Returns:
        argparse.ArgumentParser: The same parser instance with position-related arguments added.
    """

    group = parser.add_argument_group(
        "Position Configuration",
        "Arguments that modify fixed position and other position-related configuration.",
    )
    group.add_argument(
        "--setalt",
        help="Set device altitude in meters (allows use without GPS), and enable fixed position. "
        "When providing positions with `--setlat`, `--setlon`, and `--setalt`, missing values will be set to 0.",
    )

    group.add_argument(
        "--setlat",
        help="Set device latitude (allows use without GPS), and enable fixed position. Accepts a decimal value or an integer premultiplied by 1e7. "
        "When providing positions with `--setlat`, `--setlon`, and `--setalt`, missing values will be set to 0.",
    )

    group.add_argument(
        "--setlon",
        help="Set device longitude (allows use without GPS), and enable fixed position. Accepts a decimal value or an integer premultiplied by 1e7. "
        "When providing positions with `--setlat`, `--setlon`, and `--setalt`, missing values will be set to 0.",
    )

    group.add_argument(
        "--remove-position",
        help="Clear any existing fixed position and disable fixed position.",
        action="store_true",
    )

    group.add_argument(
        "--pos-fields",
        help="Specify fields to send when sending a position. Use no argument for a list of valid values. "
        "Can pass multiple values as a space separated list like "
        "this: '--pos-fields ALTITUDE HEADING SPEED'",
        nargs="*",
        action="store",
    )
    return parser


def addLocalActionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register CLI arguments for local-only actions and information display.
    
    Adds options to query or display information from the local node (radio):
    - --info: display radio configuration
    - --nodes: print the node list in a formatted table
    - --show-fields: select comma-separated fields to display with --nodes
    
    Parameters:
        parser (argparse.ArgumentParser): The argument parser to extend.
    
    Returns:
        argparse.ArgumentParser: The same parser instance with local-action arguments added.
    """
    group = parser.add_argument_group(
        "Local Actions",
        "Arguments that take actions or request information from the local node only.",
    )

    group.add_argument(
        "--info",
        help="Read and display the radio config information",
        action="store_true",
    )

    group.add_argument(
        "--nodes",
        help="Print Node List in a pretty formatted table",
        action="store_true",
    )

    group.add_argument(
        "--show-fields",
        help="Specify fields to show (comma-separated) when using --nodes",
        type=lambda s: s.split(","),
        default=None,
    )

    return parser


def addRemoteActionArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Register remote-action CLI flags on the provided ArgumentParser.
    
    Parameters:
        parser (argparse.ArgumentParser): The parser to extend with remote action arguments (e.g., sendtext, traceroute, request-telemetry, request-position, reply).
    
    Returns:
        argparse.ArgumentParser: The same parser instance with remote-action arguments added.
    """
    group = parser.add_argument_group(
        "Remote Actions",
        "Arguments that take actions or request information from either the local node or remote nodes via the mesh.",
    )

    group.add_argument(
        "--sendtext",
        help="Send a text message. Can specify a destination '--dest', use of PRIVATE_APP port '--private', and/or channel index '--ch-index'.",
        metavar="TEXT",
    )

    group.add_argument(
        "--private",
        help="Optional argument for sending text messages to the PRIVATE_APP port. Use in combination with --sendtext.",
        action="store_true",
    )

    group.add_argument(
        "--traceroute",
        help="Traceroute from connected node to a destination. "
        "You need pass the destination ID as argument, like "
        "this: '--traceroute !ba4bf9d0' | '--traceroute 0xba4bf9d0'"
        "Only nodes with a shared channel can be traced.",
        metavar="!xxxxxxxx",
    )

    group.add_argument(
        "--request-telemetry",
        help="Request telemetry from a node. With an argument, requests that specific type of telemetry.  "
        "You need to pass the destination ID as argument with '--dest'. "
        "For repeaters, the nodeNum is required.",
        action="store",
        nargs="?",
        default=None,
        const="device",
        metavar="TYPE",
    )

    group.add_argument(
        "--request-position",
        help="Request the position from a node. "
        "You need to pass the destination ID as an argument with '--dest'. "
        "For repeaters, the nodeNum is required.",
        action="store_true",
    )

    group.add_argument(
        "--reply", help="Reply to received messages", action="store_true"
    )

    return parser


def addRemoteAdminArgs(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """
    Add CLI arguments for remote administrative actions that require admin access.
    
    These options request administrative operations on a local or remote node (for
    example reboot, factory reset, node DB edits, and time setting).
    
    Returns:
        parser (argparse.ArgumentParser): The same parser with remote-admin arguments added.
    """

    outer = parser.add_argument_group(
        "Remote Admin Actions",
        "Arguments that interact with local node or remote nodes via the mesh, requiring admin access.",
    )

    group = outer.add_mutually_exclusive_group()

    group.add_argument(
        "--reboot", help="Tell the destination node to reboot", action="store_true"
    )

    group.add_argument(
        "--reboot-ota",
        help="Tell the destination node to reboot into factory firmware (ESP32)",
        action="store_true",
    )

    group.add_argument(
        "--enter-dfu",
        help="Tell the destination node to enter DFU mode (NRF52)",
        action="store_true",
    )

    group.add_argument(
        "--shutdown", help="Tell the destination node to shutdown", action="store_true"
    )

    group.add_argument(
        "--device-metadata",
        help="Get the device metadata from the node",
        action="store_true",
    )

    group.add_argument(
        "--factory-reset",
        "--factory-reset-config",
        help="Tell the destination node to install the default config, preserving BLE bonds & PKI keys",
        action="store_true",
    )

    group.add_argument(
        "--factory-reset-device",
        help="Tell the destination node to install the default config and clear BLE bonds & PKI keys",
        action="store_true",
    )

    group.add_argument(
        "--remove-node",
        help="Tell the destination node to remove a specific node from its NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--set-favorite-node",
        help="Tell the destination node to set the specified node to be favorited on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--remove-favorite-node",
        help="Tell the destination node to set the specified node to be un-favorited on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--set-ignored-node",
        help="Tell the destination node to set the specified node to be ignored on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--remove-ignored-node",
        help="Tell the destination node to set the specified node to be un-ignored on the NodeDB. "
        "Use the node ID with a '!' or '0x' prefix or the node number.",
        metavar="!xxxxxxxx",
    )
    group.add_argument(
        "--reset-nodedb",
        help="Tell the destination node to clear its list of nodes",
        action="store_true",
    )

    group.add_argument(
        "--set-time",
        help="Set the time to the provided unix epoch timestamp, or the system's current time if omitted or 0.",
        action="store",
        type=int,
        nargs="?",
        default=None,
        const=0,
        metavar="TIMESTAMP",
    )

    return parser


def initParser():
    """
    Configure the command-line argument parser and parse command-line arguments.
    
    Sets up all CLI argument groups and options, then parses sys.argv and stores
    the configured parser and parsed arguments on mt_config (mt_config.parser and
    mt_config.args). If argcomplete is available, shell autocompletion is enabled.
    """
    parser = mt_config.parser
    args = mt_config.args

    # The "Help" group includes the help option and other informational stuff about the CLI itself
    outerHelpGroup = parser.add_argument_group("Help")
    helpGroup = outerHelpGroup.add_mutually_exclusive_group()
    helpGroup.add_argument(
        "-h", "--help", action="help", help="show this help message and exit"
    )

    the_version = get_active_version()
    helpGroup.add_argument("--version", action="version", version=f"{the_version}")

    helpGroup.add_argument(
        "--support",
        action="store_true",
        help="Show support info (useful when troubleshooting an issue)",
    )

    # Connection arguments to indicate a device to connect to
    parser = addConnectionArgs(parser)

    # Selection arguments to denote nodes and channels to use
    parser = addSelectionArgs(parser)

    # Arguments concerning viewing and setting configuration
    parser = addImportExportArgs(parser)
    parser = addConfigArgs(parser)
    parser = addPositionConfigArgs(parser)
    parser = addChannelConfigArgs(parser)

    # Arguments for sending or requesting things from the local device
    parser = addLocalActionArgs(parser)

    # Arguments for sending or requesting things from the mesh
    parser = addRemoteActionArgs(parser)
    parser = addRemoteAdminArgs(parser)

    # All the rest of the arguments
    group = parser.add_argument_group("Miscellaneous arguments")

    group.add_argument(
        "--seriallog",
        help="Log device serial output to either 'none' or a filename to append to.  Defaults to '%(const)s' if no filename specified.",
        nargs="?",
        const="stdout",
        default=None,
        metavar="LOG_DESTINATION",
    )

    group.add_argument(
        "--ack",
        help="Use in combination with compatible actions (e.g. --sendtext) to wait for an acknowledgment.",
        action="store_true",
    )

    group.add_argument(
        "--timeout",
        help="How long to wait for replies. Default %(default)ss.",
        default=300,
        type=int,
        metavar="SECONDS",
    )

    group.add_argument(
        "--no-nodes",
        help="Request that the node not send node info to the client. "
        "Will break things that depend on the nodedb, but will speed up startup. Requires 2.3.11+ firmware.",
        action="store_true",
    )

    group.add_argument(
        "--debug", help="Show API library debug log messages", action="store_true"
    )

    group.add_argument(
        "--debuglib",
        help="Show only API library debug log messages",
        action="store_true",
    )

    group.add_argument(
        "--test",
        help="Run stress test against all connected Meshtastic devices",
        action="store_true",
    )

    group.add_argument(
        "--wait-to-disconnect",
        help="How many seconds to wait before disconnecting from the device.",
        const="5",
        nargs="?",
        action="store",
        metavar="SECONDS",
    )

    group.add_argument(
        "--noproto",
        help="Don't start the API, just function as a dumb serial terminal.",
        action="store_true",
    )

    group.add_argument(
        "--listen",
        help="Just stay open and listen to the protobuf stream. Enables debug logging.",
        action="store_true",
    )

    group.add_argument(
        "--no-time",
        help="Deprecated. Retained for backwards compatibility in scripts, but is a no-op.",
        action="store_true",
    )

    power_group = parser.add_argument_group(
        "Power Testing", "Options for power testing/logging."
    )

    power_supply_group = power_group.add_mutually_exclusive_group()

    power_supply_group.add_argument(
        "--power-riden",
        help="Talk to a Riden power-supply. You must specify the device path, i.e. /dev/ttyUSBxxx",
    )

    power_supply_group.add_argument(
        "--power-ppk2-meter",
        help="Talk to a Nordic Power Profiler Kit 2 (in meter mode)",
        action="store_true",
    )

    power_supply_group.add_argument(
        "--power-ppk2-supply",
        help="Talk to a Nordic Power Profiler Kit 2 (in supply mode)",
        action="store_true",
    )

    power_supply_group.add_argument(
        "--power-sim",
        help="Use a simulated power meter (for development)",
        action="store_true",
    )

    power_group.add_argument(
        "--power-voltage",
        help="Set the specified voltage on the power-supply. Be VERY careful, you can burn things up.",
    )

    power_group.add_argument(
        "--power-stress",
        help="Perform power monitor stress testing, to capture a power consumption profile for the device (also requires --power-mon)",
        action="store_true",
    )

    power_group.add_argument(
        "--power-wait",
        help="Prompt the user to wait for device reset before looking for device serial ports (some boards kill power to USB serial port)",
        action="store_true",
    )

    power_group.add_argument(
        "--slog",
        help="Store structured-logs (slogs) for this run, optionally you can specify a destination directory",
        nargs="?",
        default=None,
        const="default",
    )

    remoteHardwareArgs = parser.add_argument_group(
        "Remote Hardware", "Arguments related to the Remote Hardware module"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-wrb", nargs=2, help="Set a particular GPIO # to 1 or 0", action="append"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-rd", help="Read from a GPIO mask (ex: '0x10')"
    )

    remoteHardwareArgs.add_argument(
        "--gpio-watch", help="Start watching a GPIO mask for changes (ex: '0x10')"
    )

    have_tunnel = platform.system() == "Linux"
    if have_tunnel:
        tunnelArgs = parser.add_argument_group(
            "Tunnel", "Arguments related to establishing a tunnel device over the mesh."
        )
        tunnelArgs.add_argument(
            "--tunnel",
            action="store_true",
            help="Create a TUN tunnel device for forwarding IP packets over the mesh",
        )
        tunnelArgs.add_argument(
            "--subnet",
            dest="tunnel_net",
            help="Sets the local-end subnet address for the TUN IP bridge. (ex: 10.115' which is the default)",
            default=None,
        )

    parser.set_defaults(deprecated=None)

    if argcomplete is not None:
        argcomplete.autocomplete(parser)
    args = parser.parse_args()
    mt_config.args = args
    mt_config.parser = parser


def main():
    """
    Run the Meshtastic command-line entry point: initialize the argument parser, process CLI actions, and perform cleanup.
    
    This function initializes the global parser via initParser(), executes the shared CLI flow in common(), and closes the configured logfile if one was opened.
    """
    parser = argparse.ArgumentParser(
        add_help=False,
        epilog="If no connection arguments are specified, we search for a compatible serial device, "
        "and if none is found, then attempt a TCP connection to localhost.",
    )
    mt_config.parser = parser
    initParser()
    common()
    logfile = mt_config.logfile
    if logfile:
        logfile.close()


def tunnelMain():
    """
    Start the Meshtastic command-line tool in IP-tunnel mode.
    
    Enable tunnel mode on the parsed CLI arguments and run the shared command-line flow that establishes connections and processes requested actions.
    """
    parser = argparse.ArgumentParser(add_help=False)
    mt_config.parser = parser
    initParser()
    args = mt_config.args
    args.tunnel = True
    mt_config.args = args
    common()


if __name__ == "__main__":
    main()