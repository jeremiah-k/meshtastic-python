"""With two radios connected serially, send and receive test
messages and report back if successful.
"""

import io
import logging
import sys
import time
import traceback
from typing import Any, List, Optional

from pubsub import pub  # type: ignore[import-untyped]

import meshtastic.util
from meshtastic import BROADCAST_NUM
from meshtastic.protobuf import portnums_pb2
from meshtastic.serial_interface import SerialInterface
from meshtastic.tcp_interface import TCPInterface


class _FallbackDotMap(dict):
    """Lightweight fallback used when dotmap is unavailable."""

    def __getattr__(self, key: str) -> Any:
        """
        Provide attribute-style access for dictionary keys.

        When an attribute is accessed, return the dictionary value for that key. If the key is
        missing, return an empty _FallbackDotMap. If the value is a dict, return a _FallbackDotMap
        wrapping that dict; otherwise return the value unchanged.

        Parameters
        ----------
            key (str): Attribute name to retrieve as a dictionary key.

        Returns
        -------
            The value stored under `key`, a `_FallbackDotMap` wrapping a nested dict, or an empty `_FallbackDotMap` if the key is absent.

        """
        # Guard dunder names to avoid interfering with copy, pickle, etc.
        if key.startswith("__") and key.endswith("__"):
            raise AttributeError(key)
        try:
            value = self[key]
        except KeyError:
            # Match real DotMap's permissive behavior: return empty DotMap for missing keys
            return _FallbackDotMap()
        if isinstance(value, dict):
            return _FallbackDotMap(value)
        return value

    def __setattr__(self, key: str, value: Any) -> None:
        """
        Set dictionary keys via attribute-style access.

        Parameters
        ----------
            key (str): Attribute name to set as a dictionary key.
            value (Any): Value to store under the key.

        """
        self[key] = value

    def __delattr__(self, key: str) -> None:
        """
        Delete dictionary keys via attribute-style access.

        Parameters
        ----------
            key (str): Attribute name to delete as a dictionary key.

        Raises
        ------
            AttributeError: If the key does not exist.

        """
        try:
            del self[key]
        except KeyError:
            raise AttributeError(key) from None


DotMap: type[Any]
try:
    from dotmap import DotMap as _ImportedDotMap  # type: ignore[import-untyped]
except ImportError:
    DotMap = _FallbackDotMap
else:
    DotMap = _ImportedDotMap

"""The interfaces we are using for our tests"""
interfaces: List = []

"""A list of all packets we received while the current test was running"""
receivedPackets: Optional[List] = None

testsRunning: bool = False

testNumber: int = 0

sendingInterface = None

logger = logging.getLogger(__name__)


def onReceive(packet: dict, interface: Any) -> None:
    """
    Handle an incoming packet and record clear-text messages.

    If the packet originated from the current sendingInterface it is ignored. Otherwise the packet
    is converted to a DotMap and, when its decoded.portnum equals "TEXT_MESSAGE_APP" and the
    module-level `receivedPackets` list is set, the converted packet is appended to
    `receivedPackets`.

    Parameters
    ----------
        packet (dict): Raw packet data as received.
        interface (Any): Interface object that delivered the packet.

    """
    if sendingInterface != interface:
        # print(f"From {interface.stream.port}: {packet}")
        p = DotMap(packet)

        if p.decoded.portnum == portnums_pb2.PortNum.Name(
            portnums_pb2.PortNum.TEXT_MESSAGE_APP
        ):
            # We only care a about clear text packets
            if receivedPackets is not None:
                receivedPackets.append(p)


def onNode(node: Any) -> None:
    """
    Handle updates to the node database.

    Parameters
    ----------
        node (Any): The node entry that changed or a payload describing the change (typically a node database record).

    """
    print(f"Node changed: {node}")


def subscribe() -> None:
    """
    Subscribe to meshtastic pub/sub topics to receive node update notifications.

    Registers the onNode callback for the "meshtastic.node" topic so node-change events are delivered to onNode.
    """

    pub.subscribe(onNode, "meshtastic.node")


def testSend(
    fromInterface: Any,
    toInterface: Any,
    isBroadcast: bool = False,
    asBinary: bool = False,
    wantAck: bool = False,
) -> bool:
    """
    Send a single test packet from one interface to another.

    Parameters
    ----------
        fromInterface (Any): Interface used to send the packet.
        toInterface (Any): Interface targeted to receive the packet (ignored for broadcasts).
        isBroadcast (bool): If True, send to the broadcast address.
        asBinary (bool): If True, send the payload as binary data.
        wantAck (bool): If True, request an acknowledgment from the recipient.

    Returns
    -------
        bool: `True` if a response packet was received within 60 seconds, `False` otherwise.

    """
    # pylint: disable=W0603
    global receivedPackets
    receivedPackets = []
    fromNode = fromInterface.myInfo.my_node_num

    if isBroadcast:
        toNode = BROADCAST_NUM
    else:
        toNode = toInterface.myInfo.my_node_num

    logger.debug(f"Sending test wantAck={wantAck} packet from {fromNode} to {toNode}")
    # pylint: disable=W0603
    global sendingInterface
    sendingInterface = fromInterface
    if not asBinary:
        fromInterface.sendText(f"Test {testNumber}", toNode, wantAck=wantAck)
    else:
        fromInterface.sendData(
            (f"Binary {testNumber}").encode("utf-8"), toNode, wantAck=wantAck
        )
    for _ in range(60):  # max of 60 secs before we timeout
        time.sleep(1)
        if len(receivedPackets) >= 1:
            return True
    return False  # Failed to send


def runTests(numTests: int = 50, wantAck: bool = False, maxFailures: int = 0) -> bool:
    """
    Execute a series of send/receive test iterations and evaluate overall success.

    Parameters
    ----------
        numTests (int): Number of test iterations to run.
        wantAck (bool): If True, request acknowledgments for sent test packets.
        maxFailures (int): Maximum allowed failed tests before overall result is considered a failure.

    Returns
    -------
        bool: `True` if the number of failed tests is less than or equal to `maxFailures`, `False` otherwise.

    """
    logger.info(f"Running {numTests} tests with wantAck={wantAck}")
    numFail: int = 0
    numSuccess: int = 0
    for _ in range(numTests):
        # pylint: disable=W0603
        global testNumber
        testNumber = testNumber + 1
        isBroadcast: bool = True
        # asBinary=(i % 2 == 0)
        success = testSend(
            interfaces[0], interfaces[1], isBroadcast, asBinary=False, wantAck=wantAck
        )
        if not success:
            numFail = numFail + 1
            logger.error(
                f"Test {testNumber} failed, expected packet not received ({numFail} failures so far)"
            )
        else:
            numSuccess = numSuccess + 1
            logger.info(
                f"Test {testNumber} succeeded {numSuccess} successes {numFail} failures so far"
            )

        time.sleep(1)

    if numFail > maxFailures:
        logger.error("Too many failures! Test failed!")
        return False
    return True


def testThread(numTests: int = 50) -> bool:
    """
    Run a two-stage test sequence across discovered devices.

    First stage runs `numTests` with acknowledgments required; if that stage succeeds, a second
    stage runs `numTests` without acknowledgments and allows up to one failure.

    Parameters
    ----------
        numTests (int): Number of tests to run in each stage.

    Returns
    -------
        bool: True if the overall test sequence succeeded (both stages passed as
            required), False otherwise.

    """
    logger.info("Found devices, starting tests...")
    result: bool = runTests(numTests, wantAck=True)
    if result:
        # Run another test
        # Allow a few dropped packets
        result = runTests(numTests, wantAck=False, maxFailures=1)
    return result


def onConnection(interface: Any = None, topic: Any = pub.AUTO_TOPIC) -> None:
    """
    Notify about connection state changes by printing the topic name.

    Parameters
    ----------
        interface (Any): The interface whose connection state changed.
        topic (Any): The connection topic object or value; if it has a `getName()`
            method that name is used, otherwise `str(topic)` is printed.

    """
    _ = interface
    topic_name = topic.getName() if hasattr(topic, "getName") else str(topic)
    print(f"Connection changed: {topic_name}")


def openDebugLog(portName: str) -> io.TextIOWrapper:
    """
    Create and open a per-port debug log file.

    The filename is formed by prefixing "log" to the provided port name with any "/" characters replaced by "_".

    Parameters
    ----------
        portName (str): The serial port name used to derive the log filename.

    Returns
    -------
        io.TextIOWrapper: An open text file for writing (UTF-8, line-buffered) for the debug log.

    """
    debugname = "log" + portName.replace("/", "_")
    logger.info(f"Writing serial debugging to {debugname}")
    return open(debugname, "w+", buffering=1, encoding="utf8")


def testAll(numTests: int = 5) -> bool:
    """
    Discover connected Meshtastic devices, open interfaces, and run a series of integration tests.

    Parameters
    ----------
        numTests (int): Number of test iterations to perform.

    Returns
    -------
        True if the test run completed successfully, False otherwise.

    """
    ports: List[str] = meshtastic.util.findPorts(True)
    if len(ports) < 2:
        meshtastic.util.our_exit(
            "Warning: Must have at least two devices connected to USB."
        )

    pub.subscribe(onConnection, "meshtastic.connection")
    pub.subscribe(onReceive, "meshtastic.receive")
    # pylint: disable=W0603
    global interfaces
    interfaces = list(
        map(
            lambda port: SerialInterface(
                port, debugOut=openDebugLog(port), connectNow=True
            ),
            ports,
        )
    )

    logger.info("Ports opened, starting test")
    result: bool = testThread(numTests)

    for i in interfaces:
        i.close()

    return result


def testSimulator() -> None:
    """
    Assume that someone has launched meshtastic-native as a simulated node.
    Talk to that node over TCP, do some operations and if they are successful
    exit the process with a success code, else exit with a non zero exit code.

    Run with
    python3 -c 'from meshtastic.test import testSimulator; testSimulator()'
    """
    logging.basicConfig(level=logging.DEBUG)
    logger.info("Connecting to simulator on localhost!")
    try:
        iface: TCPInterface = TCPInterface("localhost")
        iface.showInfo()
        iface.localNode.showInfo()
        iface.localNode.exitSimulator()
        iface.close()
        logger.info("Integration test successful!")
    except OSError:
        print("Error while testing simulator:", sys.exc_info()[0])
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
