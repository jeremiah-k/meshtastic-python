"""Remote hardware."""

import logging
from typing import TYPE_CHECKING, Any, Callable

from pubsub import pub  # type: ignore[import-untyped]

from meshtastic.protobuf import portnums_pb2, remote_hardware_pb2

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

GPIO_CHANNEL_NAME = "gpio"
REMOTE_HARDWARE_TOPIC = "meshtastic.receive.remotehw"
NO_GPIO_CHANNEL_ERROR = (
    f"No channel named '{GPIO_CHANNEL_NAME}' was found. "
    f"On the sending and receiving nodes create a channel named '{GPIO_CHANNEL_NAME}'.\n"
    f"For example, run '--ch-add {GPIO_CHANNEL_NAME}' on one device, then '--seturl' on\n"
    "the other devices using the url from the device where the channel was added."
)
MISSING_DEST_NODE_ID_ERROR = (
    "Must use a destination node ID for this operation (use --dest)."
)
WATCH_MASKS_ATTR = "_remote_hardware_watch_masks"

_MESH_INTERFACE_ERROR: type[Exception] | None = None


def _get_mesh_interface_error() -> type[Exception]:
    """Resolve MeshInterfaceError lazily to avoid module-import cycles.

    Returns
    -------
    MeshInterfaceError : type[Exception]
        The cached MeshInterface.MeshInterfaceError exception class.
    """
    global _MESH_INTERFACE_ERROR  # pylint: disable=global-statement
    if _MESH_INTERFACE_ERROR is None:
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        _MESH_INTERFACE_ERROR = MeshInterface.MeshInterfaceError
    return _MESH_INTERFACE_ERROR


def _normalize_node_key(nodeid: Any) -> str | None:
    """Normalize a node identifier to a stable string key for internal mask tracking."""
    key: str | None = None
    if nodeid is None or isinstance(nodeid, bool):
        return key

    if isinstance(nodeid, int):
        key = f"num:{nodeid}"
    elif isinstance(nodeid, str):
        stripped = nodeid.strip()
        if stripped:
            if stripped.startswith("!"):
                key = f"id:{stripped.lower()}"
            else:
                try:
                    key = f"num:{int(stripped, 0)}"
                except ValueError:
                    key = f"id:{stripped.lower()}"
    else:
        try:
            key = f"num:{int(nodeid)}"
        except (TypeError, ValueError):
            key = None

    return key


def _get_watch_masks(interface: "MeshInterface") -> dict[str, int]:
    """Return per-interface watch masks, creating storage if needed."""
    watch_masks = getattr(interface, WATCH_MASKS_ATTR, None)
    if isinstance(watch_masks, dict):
        return watch_masks
    watch_masks = {}
    setattr(interface, WATCH_MASKS_ATTR, watch_masks)
    return watch_masks


def onGPIOreceive(packet: dict[str, Any], interface: "MeshInterface") -> None:
    """Handle an incoming remote hardware (GPIO) response packet, log its summary, and mark the interface as having received a response.

    Extracts `gpioValue` from packet["decoded"]["remotehw"] (defaults to 0 if
    absent), determines an active mask from packet `gpioMask` or from
    RemoteHardwareClient watch-mask state keyed by sender node (with a final
    legacy fallback to ``interface.mask``), computes the masked GPIO value,
    logs the hardware type and computed value, and sets
    `interface.gotResponse` to True.

    Parameters
    ----------
    packet : dict[str, Any]
        Decoded message dictionary containing a "remotehw" mapping with optional keys:
        - "gpioValue" (int): GPIO value reported by the remote device (may be
          omitted; treated as 0).
        - "gpioMask" (int): Mask provided by the remote device.
        - "type" (int|enum): Hardware message type.
    interface : 'MeshInterface'
        MeshInterface instance that may contain per-node watch mask state and
        whose `gotResponse` attribute will be set to True.
    """
    logger.debug("packet:%s interface:%s", packet, interface)
    gpioValue = 0
    decoded = packet.get("decoded", {})
    if not isinstance(decoded, dict):
        return
    hw = decoded.get("remotehw", {})
    if not isinstance(hw, dict):
        return
    if "gpioValue" in hw:
        gpioValue = hw["gpioValue"]
    # Note: proto3 omits zero-valued fields; gpioValue defaults to 0
    # See https://developers.google.com/protocol-buffers/docs/proto3#default

    raw_mask = hw.get("gpioMask")
    if raw_mask is None:
        watch_masks = _get_watch_masks(interface)
        for lookup_value in (packet.get("from"), packet.get("fromId")):
            key = _normalize_node_key(lookup_value)
            if key is not None and key in watch_masks:
                raw_mask = watch_masks[key]
                break
        if raw_mask is None and len(watch_masks) == 1:
            # Legacy fallback for packets that omit sender identity.
            raw_mask = next(iter(watch_masks.values()))
    if raw_mask is None:
        raw_mask = getattr(interface, "mask", None)
    if raw_mask is None:
        raw_mask = 0
    try:
        mask = int(raw_mask)
        gpio_value_int = int(gpioValue)
    except (TypeError, ValueError):
        logger.warning(
            "Could not convert gpioValue=%r or mask=%r to int.",
            gpioValue,
            raw_mask,
        )
        mask = 0
        gpio_value_int = 0
    logger.debug("mask:%s", mask)
    value = gpio_value_int & mask
    logger.info(
        "Received RemoteHardware type=%s, gpio_value=%s value=%s",
        hw.get("type", remote_hardware_pb2.HardwareMessage.Type.UNSET),
        gpioValue,
        value,
    )
    interface.gotResponse = True


class RemoteHardwareClient:
    """Client code to control and monitor simple hardware built into Meshtastic devices.

    It is intended to be both a useful API/service and example code for how you can
    connect to your own custom meshtastic services.
    """

    def __init__(self, iface: "MeshInterface") -> None:
        """Create a RemoteHardwareClient bound to a MeshInterface and subscribe to remote GPIO responses.

        Parameters
        ----------
        iface : 'MeshInterface'
            An already-open MeshInterface instance to use for sending/receiving remote hardware messages.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If the local node has no channel named "gpio".
        """
        self.iface = iface
        ch = iface.localNode.getChannelByName(GPIO_CHANNEL_NAME)
        if not ch:
            mesh_interface_error = _get_mesh_interface_error()
            raise mesh_interface_error(NO_GPIO_CHANNEL_ERROR)
        self.channelIndex = ch.index
        _get_watch_masks(self.iface)

        already_subscribed = False
        try:
            already_subscribed = pub.isSubscribed(onGPIOreceive, REMOTE_HARDWARE_TOPIC)
        except pub.TopicNameError:
            # Topic may not exist yet; subscribe below to create/register it.
            already_subscribed = False
        except (TypeError, ValueError) as ex:
            logger.warning(
                "Unable to inspect remote hardware topic subscription: %s", ex
            )
            already_subscribed = False
        if not already_subscribed:
            pub.subscribe(onGPIOreceive, REMOTE_HARDWARE_TOPIC)

    def _send_hardware(
        self,
        nodeid: int | str | None,
        r: remote_hardware_pb2.HardwareMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """Send a HardwareMessage to a remote node over the configured GPIO channel.

        Parameters
        ----------
        nodeid : int | str | None
            Destination node ID.
        r : remote_hardware_pb2.HardwareMessage
            The hardware message payload to send.
        wantResponse : bool
            If True, request and wait for a device response. (Default value = False)
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked when a response is received. (Default value = None)

        Returns
        -------
        Any
            The result returned by the underlying sendData call.

        Raises
        ------
        MeshInterface.MeshInterfaceError
            If no destination node ID is provided.
        """
        # Reject None, empty string, whitespace-only string, or integer 0
        is_invalid_str = isinstance(nodeid, str) and nodeid.strip() == ""
        is_invalid_int = isinstance(nodeid, int) and nodeid == 0
        if nodeid is None or nodeid == "" or is_invalid_str or is_invalid_int:
            mesh_interface_error = _get_mesh_interface_error()
            raise mesh_interface_error(MISSING_DEST_NODE_ID_ERROR)
        return self.iface.sendData(
            r,
            nodeid,
            portnums_pb2.REMOTE_HARDWARE_APP,
            wantAck=True,
            channelIndex=self.channelIndex,
            wantResponse=wantResponse,
            onResponse=onResponse,
        )

    def writeGPIOs(self, nodeid: int | str, mask: int, vals: int) -> Any:
        """Set specified GPIO pins on a remote device according to the provided mask and values.

        Parameters
        ----------
        nodeid : int | str
            Destination node identifier.
        mask : int
            Bitmask where bits set to 1 indicate GPIO pins to modify.
        vals : int
            Bit pattern to write to the masked GPIO pins; bits corresponding to 1s in `mask` will be applied.

        Returns
        -------
        Any
            Result of the underlying send operation.
        """
        logger.debug("writeGPIOs nodeid:%s mask:%s vals:%s", nodeid, mask, vals)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WRITE_GPIOS
        r.gpio_mask = mask
        r.gpio_value = vals
        return self._send_hardware(nodeid, r)

    def readGPIOs(
        self,
        nodeid: int | str,
        mask: int,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """Request the device to read the specified GPIO bits.

        Parameters
        ----------
        nodeid : int | str
            Destination node identifier or address to send the request to.
        mask : int
            Bitmask indicating which GPIO pins to read.
        onResponse : Callable[[dict[str, Any]], Any] | None
            Optional callback invoked with the response packet when a response arrives. (Default value = None)

        Returns
        -------
        Any
            The result of the underlying send operation (may be the callback's return value or the send call result).
        """
        logger.debug("readGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.READ_GPIOS
        r.gpio_mask = mask
        return self._send_hardware(nodeid, r, wantResponse=True, onResponse=onResponse)

    def watchGPIOs(self, nodeid: int | str, mask: int) -> Any:
        """Start monitoring the specified GPIO bits on a remote device for changes.

        Parameters
        ----------
        nodeid : int | str
            Destination node identifier for the target device.
        mask : int
            Bitmask selecting which GPIO pins to monitor (bit i corresponds to GPIO i).

        Returns
        -------
        Any
            Result of sending the watch request; may contain a send/response token or interface-specific response.
        """
        logger.debug("watchGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WATCH_GPIOS
        r.gpio_mask = mask
        result = self._send_hardware(nodeid, r)
        node_key = _normalize_node_key(nodeid)
        if node_key is not None:
            _get_watch_masks(self.iface)[node_key] = int(mask)
        return result
