"""Power stress testing support."""

import logging
import threading
import time
from typing import Any, Callable

from ..protobuf import portnums_pb2, powermon_pb2

# Stress test timing constants
DEFAULT_ACK_TIMEOUT_S = 30.0
"""Default acknowledgment timeout in seconds."""

STRESS_DURATION_BUFFER_S = 0.2
"""Additional time to wait beyond the stress duration (seconds)."""

DEFAULT_STRESS_STATE_DURATION_S = 5.0
"""Default duration for each stress state in seconds."""

def onPowerStressResponse(packet: dict[str, Any], interface: Any) -> None:
    """Handle power stress responses and mark interface as having received a response."""
    logging.debug("packet:%s interface:%s", packet, interface)
    interface.gotResponse = True


class PowerStressClient:
    """
    The client stub for talking to the firmware PowerStress module.
    """

    def __init__(self, iface: Any, node_id: int | None = None) -> None:
        """
        Create a new PowerStressClient instance.

        iface is the already open MeshInterface instance
        """
        self.iface = iface

        if node_id is None:
            node_id = iface.myInfo.my_node_num

        self.node_id = node_id
        # No need to subscribe - because we
        # pub.subscribe(onGPIOreceive, "meshtastic.receive.powerstress")

    def sendPowerStress(
        self,
        cmd: powermon_pb2.PowerStressMessage.Opcode.ValueType,
        num_seconds: float = 0.0,
        onResponse: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """Client goo for talking with the device side agent."""
        r = powermon_pb2.PowerStressMessage()
        r.cmd = cmd
        r.num_seconds = num_seconds

        return self.iface.sendData(
            r,
            self.node_id,
            portnums_pb2.POWERSTRESS_APP,
            wantAck=True,
            wantResponse=True,
            onResponse=onResponse,
            onResponseAckPermitted=True,
        )

    def syncPowerStress(
        self,
        cmd: powermon_pb2.PowerStressMessage.Opcode.ValueType,
        num_seconds: float = 0.0,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT_S,
    ) -> bool:
        """Send a power stress command and wait for the ack.

        Parameters
        ----------
        cmd : powermon_pb2.PowerStressMessage.Opcode.ValueType
            The power stress command to send.
        num_seconds : float
            Duration for timed stress commands. A value of 0.0 means
            "run-until-ack"; values below 0.0 are treated the same. (Default value = 0.0)
        ack_timeout : float
            Maximum seconds to wait for an ack when `num_seconds` is <= 0.0.
            (Default value = 30.0)

        Returns
        -------
        bool
            `True` if an ack was observed, `False` if timed out.
        """
        ack_event = threading.Event()

        def _on_response(_packet: dict[str, Any]) -> None:
            ack_event.set()

        logging.info(
            "Sending power stress command %s",
            powermon_pb2.PowerStressMessage.Opcode.Name(cmd),
        )
        effective_num_seconds = num_seconds
        if num_seconds < 0.0:
            logging.warning(
                "Negative num_seconds=%s is invalid; treating as run-until-ack",
                num_seconds,
            )
            effective_num_seconds = 0.0

        self.sendPowerStress(
            cmd, onResponse=_on_response, num_seconds=effective_num_seconds
        )

        if effective_num_seconds <= 0.0:
            # Wait for the response and then continue, with a safety timeout.
            if not ack_event.wait(timeout=ack_timeout):
                logging.error("Timed out waiting for power stress ack!")
                return False
        else:
            # we wait a little bit longer than the time the UUT would be waiting (to make sure all of its messages are handled first)
            time.sleep(
                effective_num_seconds + STRESS_DURATION_BUFFER_S
            )  # completely block our thread for the duration of the test
            if not ack_event.is_set():
                logging.error("Did not receive ack for power stress command!")
                return False
        return True


class PowerStress:
    """Walk the UUT through a set of power states so we can capture repeatable power consumption measurements."""

    def __init__(self, iface: Any) -> None:
        self.client = PowerStressClient(iface)
        self.states: list[powermon_pb2.PowerStressMessage.Opcode.ValueType] = [
            powermon_pb2.PowerStressMessage.LED_ON,
            powermon_pb2.PowerStressMessage.LED_OFF,
            powermon_pb2.PowerStressMessage.BT_OFF,
            powermon_pb2.PowerStressMessage.BT_ON,
            powermon_pb2.PowerStressMessage.CPU_FULLON,
            powermon_pb2.PowerStressMessage.CPU_IDLE,
            # FIXME - can't test deepsleep yet because the ttyACM device disappears.
            # Fix the python code to retry connections.
            # powermon_pb2.PowerStressMessage.CPU_DEEPSLEEP,
        ]

    def run(self) -> None:
        """Run the power stress test."""
        try:
            if not self.client.syncPowerStress(
                powermon_pb2.PowerStressMessage.PRINT_INFO
            ):
                logging.warning(
                    "Ack not received for PRINT_INFO; continuing with stress sequence."
                )

            num_seconds = DEFAULT_STRESS_STATE_DURATION_S
            for s in self.states:
                s_name = powermon_pb2.PowerStressMessage.Opcode.Name(s)
                logging.info(
                    "Running power stress test %s for %s seconds",
                    s_name,
                    num_seconds,
                )
                if not self.client.syncPowerStress(s, num_seconds):
                    logging.warning("Ack not received for %s; aborting run.", s_name)
                    return

            logging.info("Power stress test complete.")
        except KeyboardInterrupt as e:
            logging.warning("Power stress interrupted: %s", e)
