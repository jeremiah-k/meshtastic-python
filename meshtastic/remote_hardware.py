"""Remote hardware."""

import logging
from typing import TYPE_CHECKING, Any, Callable, Type

from pubsub import pub  # type: ignore[import-untyped]

from meshtastic.protobuf import portnums_pb2, remote_hardware_pb2

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)

NO_GPIO_CHANNEL_ERROR = (
    "No channel named 'gpio' was found. "
    "On the sending and receiving nodes create a channel named 'gpio'."
)

_MESH_INTERFACE_ERROR: Type[Exception] | None = None


def _get_mesh_interface_error() -> Type[Exception]:
    """
    Resolve MeshInterfaceError lazily to avoid module-import cycles.
    
    Returns:
        MeshInterfaceError (Type[Exception]): The cached MeshInterface.MeshInterfaceError exception class.
    """
    global _MESH_INTERFACE_ERROR  # pylint: disable=global-statement
    if _MESH_INTERFACE_ERROR is None:
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        _MESH_INTERFACE_ERROR = MeshInterface.MeshInterfaceError
    return _MESH_INTERFACE_ERROR


def onGPIOreceive(packet: dict[str, Any], interface: "MeshInterface") -> None:
    """
    Handle an incoming remote hardware (GPIO) response packet, log its summary, and mark the interface as having received a response.
    
    Extracts `gpioValue` from packet["decoded"]["remotehw"] (defaults to 0 if absent), determines an active mask from `interface.mask` or the packet's `gpioMask`, computes the masked GPIO value, logs the hardware type and computed value, and sets `interface.gotResponse` to True.
    
    Parameters:
        packet (dict[str, Any]): Decoded message dictionary containing a "remotehw" mapping with optional keys:
            - "gpioValue" (int): GPIO value reported by the remote device (may be omitted; treated as 0).
            - "gpioMask" (int): Mask provided by the remote device.
            - "type" (int|enum): Hardware message type.
        interface (MeshInterface): MeshInterface instance whose `mask` may override the packet mask and whose `gotResponse` attribute will be set to True.
    """
    logger.debug("packet:%s interface:%s", packet, interface)
    gpioValue = 0
    hw = packet["decoded"]["remotehw"]
    if "gpioValue" in hw:
        gpioValue = hw["gpioValue"]
    # Note: proto3 omits zero-valued fields; gpioValue defaults to 0
    # See https://developers.google.com/protocol-buffers/docs/proto3#default

    mask = interface.mask if interface.mask is not None else hw.get("gpioMask", 0)
    mask = int(mask)
    logger.debug("mask:%s", mask)
    value = int(gpioValue) & mask
    logger.info(
        "Received RemoteHardware type=%s, gpio_value=%s value=%s",
        hw.get("type", remote_hardware_pb2.HardwareMessage.Type.UNSET),
        gpioValue,
        value,
    )
    interface.gotResponse = True


class RemoteHardwareClient:
    """
    Client code to control/monitor simple hardware built into the
    meshtastic devices. It is intended to be both a useful API/service and example
    code for how you can connect to your own custom meshtastic services.
    """

    def __init__(self, iface: "MeshInterface") -> None:
        """
        Create a RemoteHardwareClient bound to a MeshInterface and subscribe to remote GPIO responses.
        
        Parameters:
            iface (MeshInterface): An already-open MeshInterface instance to use for sending/receiving remote hardware messages.
        
        Raises:
            MeshInterface.MeshInterfaceError: If the local node has no channel named "gpio".
        """
        self.iface = iface
        ch = iface.localNode.getChannelByName("gpio")
        if not ch:
            raise _get_mesh_interface_error()(NO_GPIO_CHANNEL_ERROR)
        self.channelIndex = ch.index

        pub.subscribe(onGPIOreceive, "meshtastic.receive.remotehw")

    def _sendHardware(
        self,
        nodeid: int | str,
        r: remote_hardware_pb2.HardwareMessage,
        wantResponse: bool = False,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """
        Send a HardwareMessage to a remote node over the configured GPIO channel.
        
        Parameters:
            nodeid (int | str): Destination node ID.
            r (remote_hardware_pb2.HardwareMessage): The hardware message payload to send.
            wantResponse (bool): If True, request and wait for a device response.
            onResponse (Callable[[dict[str, Any]], Any] | None): Optional callback invoked when a response is received.
        
        Returns:
            Any: The result returned by the underlying sendData call.
        
        Raises:
            MeshInterface.MeshInterfaceError: If no destination node ID is provided.
        """
        if not nodeid:
            raise _get_mesh_interface_error()(
                "Must use a destination node ID for this operation."
            )
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
        """
        Set specified GPIO pins on a remote device according to the provided mask and values.
        
        Parameters:
            nodeid (int | str): Destination node identifier.
            mask (int): Bitmask where bits set to 1 indicate GPIO pins to modify.
            vals (int): Bit pattern to write to the masked GPIO pins; bits corresponding to 1s in `mask` will be applied.
        
        Returns:
            Any: Result of the underlying send operation.
        """
        logger.debug("writeGPIOs nodeid:%s mask:%s vals:%s", nodeid, mask, vals)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WRITE_GPIOS
        r.gpio_mask = mask
        r.gpio_value = vals
        return self._sendHardware(nodeid, r)

    def readGPIOs(
        self,
        nodeid: int | str,
        mask: int,
        onResponse: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """
        Request the device to read the specified GPIO bits.
        
        Parameters:
            nodeid (int | str): Destination node identifier or address to send the request to.
            mask (int): Bitmask indicating which GPIO pins to read.
            onResponse (Callable[[dict[str, Any]], Any] | None): Optional callback invoked with the response packet when a response arrives.
        
        Returns:
            Any: The result of the underlying send operation (may be the callback's return value or the send call result).
        """
        logger.debug("readGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.READ_GPIOS
        r.gpio_mask = mask
        return self._sendHardware(nodeid, r, wantResponse=True, onResponse=onResponse)

    def watchGPIOs(self, nodeid: int | str, mask: int) -> Any:
        """
        Start monitoring the specified GPIO bits on a remote device for changes.
        
        Parameters:
            nodeid (int | str): Destination node identifier for the target device.
            mask (int): Bitmask selecting which GPIO pins to monitor (bit i corresponds to GPIO i).
        
        Returns:
            Any: Result of sending the watch request; may contain a send/response token or interface-specific response.
        """
        logger.debug("watchGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WATCH_GPIOS
        r.gpio_mask = mask
        self.iface.mask = mask
        return self._sendHardware(nodeid, r)