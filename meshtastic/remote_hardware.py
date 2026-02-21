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
    """Resolve MeshInterfaceError lazily to avoid module-import cycles."""
    global _MESH_INTERFACE_ERROR  # pylint: disable=global-statement
    if _MESH_INTERFACE_ERROR is None:
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        _MESH_INTERFACE_ERROR = MeshInterface.MeshInterfaceError
    return _MESH_INTERFACE_ERROR


def onGPIOreceive(packet: dict[str, Any], interface: "MeshInterface") -> None:
    """Handle received GPIO responses."""
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
        Initialize the RemoteHardwareClient with the given MeshInterface instance.

        Parameters
        ----------
            iface: The already open MeshInterface instance.
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
        Write the specified vals bits to the device GPIOs.  Only bits in mask that
        are 1 will be changed.
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
        """Read the specified bits from GPIO inputs on the device."""
        logger.debug("readGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.READ_GPIOS
        r.gpio_mask = mask
        return self._sendHardware(nodeid, r, wantResponse=True, onResponse=onResponse)

    def watchGPIOs(self, nodeid: int | str, mask: int) -> Any:
        """Watch the specified bits from GPIO inputs on the device for changes."""
        logger.debug("watchGPIOs nodeid:%s mask:%s", nodeid, mask)
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WATCH_GPIOS
        r.gpio_mask = mask
        self.iface.mask = mask
        return self._sendHardware(nodeid, r)
