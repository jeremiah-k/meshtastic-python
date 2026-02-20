"""Remote hardware."""

import logging
from typing import TYPE_CHECKING, Any, Dict

from pubsub import pub  # type: ignore[import-untyped]

from meshtastic.protobuf import portnums_pb2, remote_hardware_pb2

if TYPE_CHECKING:
    from meshtastic.mesh_interface import MeshInterface

logger = logging.getLogger(__name__)


def onGPIOreceive(packet: Dict[str, Any], interface: "MeshInterface") -> None:
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
        hw["type"],
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

    def __init__(self, iface) -> None:
        """
        Initialize the RemoteHardwareClient with the given MeshInterface instance.

        Parameters
        ----------
            iface: The already open MeshInterface instance.
        """
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        self.iface = iface
        ch = iface.localNode.getChannelByName("gpio")
        if not ch:
            raise MeshInterface.MeshInterfaceError(
                "No channel named 'gpio' was found. "
                "On the sending and receiving nodes create a channel named 'gpio'."
            )
        self.channelIndex = ch.index

        pub.subscribe(onGPIOreceive, "meshtastic.receive.remotehw")

    def _sendHardware(self, nodeid, r, wantResponse=False, onResponse=None):
        from meshtastic.mesh_interface import (  # pylint: disable=import-outside-toplevel
            MeshInterface,
        )

        if not nodeid:
            raise MeshInterface.MeshInterfaceError(
                r"Must use a destination node ID for this operation (use --dest \!xxxxxxxxx)"
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

    def writeGPIOs(self, nodeid, mask, vals):
        """
        Write the specified vals bits to the device GPIOs.  Only bits in mask that
        are 1 will be changed.
        """
        logger.debug(f"writeGPIOs nodeid:{nodeid} mask:{mask} vals:{vals}")
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WRITE_GPIOS
        r.gpio_mask = mask
        r.gpio_value = vals
        return self._sendHardware(nodeid, r)

    def readGPIOs(self, nodeid, mask, onResponse=None):
        """Read the specified bits from GPIO inputs on the device."""
        logger.debug(f"readGPIOs nodeid:{nodeid} mask:{mask}")
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.READ_GPIOS
        r.gpio_mask = mask
        return self._sendHardware(nodeid, r, wantResponse=True, onResponse=onResponse)

    def watchGPIOs(self, nodeid, mask):
        """Watch the specified bits from GPIO inputs on the device for changes."""
        logger.debug(f"watchGPIOs nodeid:{nodeid} mask:{mask}")
        r = remote_hardware_pb2.HardwareMessage()
        r.type = remote_hardware_pb2.HardwareMessage.Type.WATCH_GPIOS
        r.gpio_mask = mask
        self.iface.mask = mask
        return self._sendHardware(nodeid, r)
