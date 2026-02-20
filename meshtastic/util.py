"""Utility functions."""

import base64
import logging
import os
import platform
import re
import subprocess
import threading
import time
import warnings
from queue import Queue
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NoReturn,
    Optional,
    Set,
    Tuple,
    Union,
)

import packaging.version as pkg_version
import requests
import serial  # type: ignore[import-untyped]
import serial.tools.list_ports  # type: ignore[import-untyped]
from google.protobuf.json_format import MessageToJson
from google.protobuf.message import Message

from meshtastic.supported_device import SupportedDevice, supported_devices
from meshtastic.version import get_active_version

"""Some devices such as a seger jlink or st-link we never want to accidentally open
     0483 STMicroelectronics ST-LINK/V2
     0136 SEGGER J-Link
     1915 NordicSemi (PPK2)
     0925 Lakeview Research Saleae Logic (logic analyzer)
04b4:602a Cypress Semiconductor Corp. Hantek DSO-6022BL (oscilloscope)
"""
blacklistVids: Set[int] = {0x1366, 0x0483, 0x1915, 0x0925, 0x04B4}

"""Some devices are highly likely to be meshtastic.
0x239a RAK4631
0x303a Heltec tracker"""
whitelistVids: Set[int] = {0x239A, 0x303A}

logger = logging.getLogger(__name__)

DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==".encode("utf-8"))


def quoteBooleans(a_string: str) -> str:
    """
    Convert boolean literals in a string to quoted boolean tokens.

    Replaces occurrences of ": true" with ": 'true'" and ": false" with ": 'false'".
    Matching is case-sensitive and only affects those exact substrings.

    Returns
    -------
        The modified string with quoted boolean tokens.

    """
    tmp: str = a_string.replace(": true", ": 'true'")
    tmp = tmp.replace(": false", ": 'false'")
    return tmp


def genPSK256() -> bytes:
    """
    Generate a 32-byte random preshared key.

    Returns
    -------
        psk (bytes): 32 bytes of cryptographically secure random data to use as a PSK.

    """
    return os.urandom(32)


def fromPSK(valstr: str) -> Any:
    """
    Parse a user-provided PSK specification into the internal PSK byte representation.

    Recognizes these special forms:
    - "random": generate and return a new 32-byte random PSK.
    - "none": return a single zero byte to indicate no encryption.
    - "default": return a single byte with value 1 to indicate the default channel PSK.
    - "simpleN": where N is an integer; return a single byte with value (N + 1).

    For any other input, parse using general string-to-value rules (e.g., hex, base64,
    numeric, boolean, or plain string) and return the corresponding value.

    Parameters
    ----------
        valstr (str): PSK specification string provided by the user.

    Returns
    -------
        bytes or other parsed value: The PSK as bytes for recognized PSK forms;
        otherwise the parsed value according to general string parsing rules
        (hex/base64/numeric/boolean/string).

    """
    if valstr == "random":
        return genPSK256()
    elif valstr == "none":
        return bytes([0])  # Use the 'no encryption' PSK
    elif valstr == "default":
        return bytes([1])  # Use default channel psk
    elif valstr.startswith("simple"):
        # Use one of the single byte encodings
        return bytes([int(valstr[6:]) + 1])
    else:
        return fromStr(valstr)


def fromStr(valstr: str) -> Any:
    """
    Convert a user-provided string into an appropriate Python value.

    The function interprets the input as follows:
    - An empty string -> empty bytes.
    - Strings starting with "0x" -> hex-decoded bytes (odd-length hex is left-padded with a leading zero nibble).
    - Strings starting with "base64:" -> base64-decoded bytes of the remainder.
    - Case-insensitive "t", "true", "yes" -> `True`; "f", "false", "no" -> `False`.
    - Otherwise attempts integer parsing, then float parsing, and falls back to the original string.

    Parameters
    ----------
        valstr (str): The input string to parse.

    Returns
    -------
        An int, float, bool, bytes, or str corresponding to the interpreted value.

    """
    val: Any
    if len(valstr) == 0:  # Treat an emptystring as an empty bytes
        val = bytes()
    elif valstr.startswith("0x"):
        # Parse hex and preserve compatibility with "0x"/single-nibble forms.
        hex_value = valstr[2:]
        if len(hex_value) == 0:
            hex_value = "00"
        elif len(hex_value) % 2 == 1:
            hex_value = "0" + hex_value
        val = bytes.fromhex(hex_value)
    elif valstr.startswith("base64:"):
        val = base64.b64decode(valstr[7:])
    elif valstr.lower() in {"t", "true", "yes"}:
        val = True
    elif valstr.lower() in {"f", "false", "no"}:
        val = False
    else:
        try:
            val = int(valstr)
        except ValueError:
            try:
                val = float(valstr)
            except ValueError:
                val = valstr  # Not a float or an int, assume string
    return val


def toStr(raw_value: Any) -> str:
    """
    Convert a value into a string suitable for config storage.

    If `raw_value` is `bytes`, returns `"base64:"` followed by its base64 encoding; otherwise returns `str(raw_value)`.

    Returns
    -------
        A string representation suitable for storing in configuration, e.g. `"base64:<data>"` for bytes.

    """
    if isinstance(raw_value, bytes):
        return "base64:" + base64.b64encode(raw_value).decode("utf-8")
    return str(raw_value)


def pskToString(psk: bytes) -> str:
    """
    Return a privacy-preserving label for a preshared key (PSK).

    Parameters
    ----------
        psk (bytes): PSK byte sequence to describe.

    Returns
    -------
        str: One of:
            - `"unencrypted"` for an empty PSK or a single zero byte,
            - `"default"` for a single byte equal to 1,
            - `"simpleN"` for a single byte >1 where N is the byte value minus one,
            - `"secret"` for any multi-byte PSK.

    """
    if len(psk) == 0:
        return "unencrypted"
    elif len(psk) == 1:
        b = psk[0]
        if b == 0:
            return "unencrypted"
        elif b == 1:
            return "default"
        else:
            return f"simple{b - 1}"
    else:
        return "secret"


def stripnl(s: Any) -> str:
    """
    Normalize input by replacing newlines with spaces and collapsing consecutive whitespace into single spaces.

    Parameters
    ----------
        s (Any): Value to normalize; will be converted to a string.

    Returns
    -------
        str: Single-line string with no newline characters and with consecutive whitespace collapsed to single spaces.

    """
    s = str(s).replace("\n", " ")
    return " ".join(s.split())


class FixmeError(Exception):
    """Exception for marking code that needs to be fixed."""


def fixme(message: str) -> NoReturn:
    """
    Raise a FixmeError with a prefixed message indicating a required fix.

    Raises
    ------
        FixmeError: Always raised with the message prefixed by "FIXME: ".

    """
    raise FixmeError("FIXME: " + message)


def catchAndIgnore(reason: str, closure: Callable[[], Any]) -> None:
    """
    Execute a callable and suppress any exception it raises, logging the failure.

    Parameters
    ----------
        reason (str): Contextual message included in the log if the callable raises an exception.
        closure (Callable[[], Any]): Zero-argument callable to execute; its exceptions are caught and logged.

    """
    try:
        closure()
    except Exception:
        logger.exception(f"Exception thrown in {reason}")


def findPorts(eliminate_duplicates: bool = False) -> List[str]:
    """
    Return a sorted list of serial port device paths that may correspond to Meshtastic devices.

    If any connected ports have vendor IDs in whitelistVids, only those ports are returned;
    otherwise returns all ports whose vendor IDs are not in blacklistVids.
    If eliminate_duplicates is True, runs eliminate_duplicate_port on the result before returning.

    Parameters
    ----------
        eliminate_duplicates (bool): If True, reduce likely-duplicate port entries.

    Returns
    -------
        List[str]: Sorted list of device path strings.

    """
    all_ports = serial.tools.list_ports.comports()

    # look for 'likely' meshtastic devices
    ports: List = list(
        map(
            lambda port: port.device,
            filter(
                lambda port: port.vid is not None and port.vid in whitelistVids,
                all_ports,
            ),
        )
    )

    # if no likely devices, just list everything not blacklisted
    if len(ports) == 0:
        ports = list(
            map(
                lambda port: port.device,
                filter(
                    lambda port: port.vid is not None and port.vid not in blacklistVids,
                    all_ports,
                ),
            )
        )

    ports.sort()
    if eliminate_duplicates:
        ports = eliminate_duplicate_port(ports)
    return ports


class DotDict(dict):
    """dot.notation access to dictionary attributes."""

    __getattr__ = dict.get
    __setattr__ = dict.__setitem__  # type: ignore[assignment]
    __delattr__ = dict.__delitem__  # type: ignore[assignment]


class dotdict(DotDict):  # pylint: disable=invalid-name
    """Backward-compatible deprecated alias for DotDict."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "dotdict is deprecated; use DotDict instead",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)


class Timeout:
    """Timeout class."""

    def __init__(self, maxSecs: float = 20.0) -> None:
        """
        Initialize a Timeout helper that manages expiration timing and polling intervals.

        Parameters
        ----------
            maxSecs (float): Maximum number of seconds to wait before the timeout expires (default 20.0).

        Attributes
        ----------
            expireTime (float): Timestamp when the timeout will expire (initially 0.0).
            sleepInterval (float): Polling sleep interval in seconds (default 0.1).
            expireTimeout (float): Configured timeout duration in seconds (set from `maxSecs`).

        """
        self.expireTime: float = 0.0
        self.sleepInterval: float = 0.1
        self.expireTimeout: float = maxSecs

    def reset(self, expireTimeout=None):
        """
        Reset the expiration time used by wait loops.

        Parameters
        ----------
            expireTimeout (float, optional): Number of seconds from now until expiration. If omitted, uses the instance's configured expireTimeout.

        """
        self.expireTime = time.time() + (
            self.expireTimeout if expireTimeout is None else expireTimeout
        )

    def waitForSet(self, target, attrs=()) -> bool:
        """
        Wait until the specified attributes on `target` are present and truthy or until the timeout expires.

        Parameters
        ----------
            target (object): Object whose attributes will be checked.
            attrs (iterable[str]): Names of attributes to wait for; if empty, the function returns immediately.

        Returns
        -------
            `true` if all named attributes are present on `target` and evaluate to true before the timeout, `false` otherwise.

        """
        self.reset()
        while time.time() < self.expireTime:
            if all(map(lambda a: getattr(target, a, None), attrs)):
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForAckNak(
        self, acknowledgment, attrs=("receivedAck", "receivedNak", "receivedImplAck")
    ) -> bool:
        """
        Wait until any of the specified acknowledgment flags becomes set or the timeout expires.

        Parameters
        ----------
                acknowledgment: An Acknowledgment-like object whose boolean attributes are checked.
                attrs (tuple[str]): Attribute names to check on `acknowledgment` (defaults to ("receivedAck", "receivedNak", "receivedImplAck")).

        Returns
        -------
                True if any specified acknowledgment flag was set before the timeout elapsed, False otherwise.

        Side effects:
                Resets the `acknowledgment` (calls its `reset()` method) when a flag is observed.

        """
        self.reset()
        while time.time() < self.expireTime:
            if any(map(lambda a: getattr(acknowledgment, a, None), attrs)):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForTraceRoute(
        self,
        waitFactor: float,
        acknowledgment: "Acknowledgment",
        attr: str = "receivedTraceRoute",
    ) -> bool:
        """
        Wait until a traceroute acknowledgment flag on the provided acknowledgment object becomes set or the adjusted timeout expires.

        Parameters
        ----------
            waitFactor (float): Multiplier applied to this Timeout's configured expire timeout before waiting.
            acknowledgment (Acknowledgment): Object whose attribute named by `attr` will be polled.
            attr (str): Attribute name on `acknowledgment` to check (default "receivedTraceRoute").

        Returns
        -------
            True if the specified acknowledgment attribute became set before the timeout expired, False otherwise.

        Notes
        -----
            On success this method calls `acknowledgment.reset()` to clear acknowledgment flags.

        """
        self.reset(self.expireTimeout * waitFactor)
        while time.time() < self.expireTime:
            if getattr(acknowledgment, attr, None):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForTelemetry(self, acknowledgment) -> bool:
        """Block until telemetry response is received. Returns True if telemetry response has been received."""
        self.reset()
        while time.time() < self.expireTime:
            if getattr(acknowledgment, "receivedTelemetry", None):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForPosition(self, acknowledgment) -> bool:
        """Block until position response is received. Returns True if position response has been received."""
        self.reset()
        while time.time() < self.expireTime:
            if getattr(acknowledgment, "receivedPosition", None):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False

    def waitForWaypoint(self, acknowledgment) -> bool:
        """
        Wait until a waypoint acknowledgement is observed or the timeout expires.

        Parameters
        ----------
            acknowledgment: Object that exposes a boolean `receivedWaypoint` attribute and a `reset()`
            method; the attribute is polled and reset on success.

        Returns
        -------
            True if a waypoint acknowledgement was received before the timeout, False otherwise.

        """
        self.reset()
        while time.time() < self.expireTime:
            if getattr(acknowledgment, "receivedWaypoint", None):
                acknowledgment.reset()
                return True
            time.sleep(self.sleepInterval)
        return False


class Acknowledgment:
    """A class that records which type of acknowledgment was just received, if any."""

    def __init__(self) -> None:
        """
        Create an Acknowledgment instance with all acknowledgment flags initialized to False.

        Tracks the following boolean flags: receivedAck, receivedNak, receivedImplAck,
        receivedTraceRoute, receivedTelemetry, receivedPosition, and receivedWaypoint.
        """
        self.receivedAck = False
        self.receivedNak = False
        self.receivedImplAck = False
        self.receivedTraceRoute = False
        self.receivedTelemetry = False
        self.receivedPosition = False
        self.receivedWaypoint = False

    def reset(self) -> None:
        """
        Clear all acknowledgment flags on this Acknowledgment instance.

        This method sets each tracked flag (receivedAck, receivedNak, receivedImplAck,
        receivedTraceRoute, receivedTelemetry, receivedPosition, receivedWaypoint) to False.
        """
        self.receivedAck = False
        self.receivedNak = False
        self.receivedImplAck = False
        self.receivedTraceRoute = False
        self.receivedTelemetry = False
        self.receivedPosition = False
        self.receivedWaypoint = False


class DeferredExecution:
    """A thread that accepts closures to run, and runs them as they are received."""

    def __init__(self, name) -> None:
        """
        Create a DeferredExecution instance and start its daemon worker thread.

        Initializes an internal work queue and launches a daemon thread (named by
        the `name` parameter) that runs the instance's _run method to process queued work.

        Parameters
        ----------
            name (str): Name assigned to the worker thread.

        """
        self.queue: Queue = Queue()
        # this thread must be marked as daemon, otherwise it will prevent clients from exiting
        self.thread = threading.Thread(
            target=self._run, args=(), name=name, daemon=True
        )
        self.thread.start()

    def queueWork(self, runnable) -> None:
        """
        Enqueue a callable to be executed by the background worker thread.

        Parameters
        ----------
            runnable (Callable[[], Any]): A zero-argument callable to be executed later.

        """
        self.queue.put(runnable)

    def _run(self) -> None:
        """
        Worker loop that continuously retrieves callables from the internal queue and executes them.

        This method runs forever in the daemon thread; it logs any exception raised by a callable and continues processing remaining work.
        """
        while True:
            try:
                o = self.queue.get()
                o()
            except Exception:
                logger.exception("Unexpected error in deferred execution")


def remove_keys_from_dict(keys: Union[Tuple, List, Set], adict: Dict) -> Dict:
    """
    Remove specified keys from a dictionary and its nested dictionaries.

    Parameters
    ----------
        keys (Union[Tuple, List, Set]): Iterable of keys to remove from the dictionary and any nested dict values.
        adict (Dict): Dictionary to process; entries matching any key in `keys` will be deleted. This dictionary is modified in place.

    Returns
    -------
        Dict: The same dictionary `adict` after removal of matching keys.

    """
    for key in keys:
        try:
            del adict[key]
        except KeyError:
            pass
    for val in adict.values():
        if isinstance(val, dict):
            remove_keys_from_dict(keys, val)
    return adict


def channel_hash(data: bytes) -> int:
    """
    Compute an XOR-based hash of the given byte sequence for channel selection.

    Returns
    -------
        int: Integer hash produced by XORing all bytes in `data`.

    """
    result = 0
    for char in data:
        result ^= char
    return result


def generate_channel_hash(name: Union[str, bytes], key: Union[str, bytes]) -> int:
    """
    Compute a channel number by hashing a channel name and a preshared key.

    Parameters
    ----------
        name (str | bytes): Channel name as a UTF-8 string or raw bytes.
        key (str | bytes): PSK provided as raw bytes or a base64 string (URL-safe '-'/'_' accepted).
        If the key is a single byte, it is combined with DEFAULT_KEY to form a full key.

    Returns
    -------
        int: Channel hash computed by XOR-ing the name hash and key hash.

    """
    # Handle key as str or bytes
    if isinstance(key, str):
        key = base64.b64decode(key.replace("-", "+").replace("_", "/").encode("utf-8"))

    if len(key) == 1:
        key = DEFAULT_KEY[:-1] + key

    # Handle name as str or bytes
    if isinstance(name, str):
        name = name.encode("utf-8")

    h_name = channel_hash(name)
    h_key = channel_hash(key)
    result: int = h_name ^ h_key
    return result


def hexstr(barray: bytes) -> str:
    """
    Convert a byte sequence to a colon-separated lowercase hex string.

    Returns
    -------
        str: Colon-separated two-digit lowercase hexadecimal pairs representing the input bytes (e.g., "01:ab:ff").

    """
    return ":".join(f"{x:02x}" for x in barray)


def ipstr(barray: bytes) -> str:
    r"""
    Produce a dotted-decimal IPv4-style string representation of a byte sequence.

    Returns
    -------
        A string of decimal octets separated by dots, one for each byte in `barray` (e.g., b'\xc0\xa8\x01\x01' -> "192.168.1.1").

    """
    return ".".join(f"{x}" for x in barray)


def readnet_u16(p: Union[bytes, bytearray, memoryview], offset: int) -> int:
    """
    Read an unsigned 16-bit big-endian integer from a buffer at a byte offset.

    Parameters
    ----------
        p (bytes | bytearray | memoryview): Buffer containing at least two bytes at the given offset.
        offset (int): Byte index within `p` where the 2-byte big-endian value starts.

    Returns
    -------
        int: The 16-bit unsigned integer read from `p[offset:offset+2]`.

    """
    return p[offset] * 256 + p[offset + 1]


def convert_mac_addr(val: str) -> str:
    """
    Convert a value into a colon-separated MAC address string.

    If `val` already matches a hexadecimal MAC address format (with optional separators),
    it is returned unchanged; otherwise `val` is interpreted as base64-encoded bytes and
    decoded to a colon-separated hexadecimal MAC string (e.g., 'fd:cd:20:17:28:5b').

    Parameters
    ----------
        val (str): A hexadecimal MAC-like string or a base64-encoded byte string.

    Returns
    -------
        str: A colon-separated hexadecimal MAC address.

    """
    if not re.match("[0-9a-f]{2}([-:]?)[0-9a-f]{2}(\\1[0-9a-f]{2}){4}$", val):
        val_as_bytes: bytes = base64.b64decode(val)
        return hexstr(val_as_bytes)
    return val


def snake_to_camel(a_string: str) -> str:
    """
    Convert a snake_case identifier to camelCase.

    Parameters
    ----------
        a_string (str): Input string in snake_case.

    Returns
    -------
        str: camelCase version of the input string.

    """
    # split underscore using split
    temp = a_string.split("_")
    # joining result
    result = temp[0] + "".join(ele.title() for ele in temp[1:])
    return result


def camel_to_snake(a_string: str) -> str:
    """Convert camelCase to snake_case."""
    return "".join(["_" + i.lower() if i.isupper() else i for i in a_string]).lstrip(
        "_"
    )


def detect_supported_devices() -> Set:
    """
    Detect supported devices present on the host by vendor ID.

    Queries the host OS for attached USB devices (using lsusb on Linux, PowerShell Get-PnpDevice
    on Windows, and system_profiler on macOS) and matches discovered vendor IDs against the module's
    known supported vendor IDs. Returns a set of supported device descriptors for any matching devices;
    returns an empty set if none are found.

    Returns
    -------
        Set: A set of supported device entries (one per matching supported device).

    """
    system: str = platform.system()
    # print(f'system:{system}')

    possible_devices = set()
    if system == "Linux":
        # if linux, run lsusb and list ports

        # linux: use lsusb
        # Bus 001 Device 091: ID 10c4:ea60 Silicon Labs CP210x UART Bridge
        _, lsusb_output = subprocess.getstatusoutput("lsusb")
        vids = get_unique_vendor_ids()
        for vid in vids:
            # print(f'looking for {vid}...')
            search = f" {vid}:"
            # print(f'search:"{search}"')
            if re.search(search, lsusb_output, re.MULTILINE):
                # print(f'Found vendor id that matches')
                devices = get_devices_with_vendor_id(vid)
                for device in devices:
                    possible_devices.add(device)

    elif system == "Windows":
        # if windows, run Get-PnpDevice
        _, sp_output = subprocess.getstatusoutput(
            'powershell.exe "[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8;'
            'Get-PnpDevice -PresentOnly | Format-List"'
        )
        # print(f'sp_output:{sp_output}')
        vids = get_unique_vendor_ids()
        for vid in vids:
            # print(f'looking for {vid.upper()}...')
            search = f"DeviceID.*{vid.upper()}&"
            # search = f'{vid.upper()}'
            # print(f'search:"{search}"')
            if re.search(search, sp_output, re.MULTILINE):
                # print(f'Found vendor id that matches')
                devices = get_devices_with_vendor_id(vid)
                for device in devices:
                    possible_devices.add(device)

    elif system == "Darwin":
        # run: system_profiler SPUSBDataType
        # Note: If in boot mode, the 19003 reports same product ID as 5005.

        _, sp_output = subprocess.getstatusoutput("system_profiler SPUSBDataType")
        vids = get_unique_vendor_ids()
        for vid in vids:
            # print(f'looking for {vid}...')
            search = f"Vendor ID: 0x{vid}"
            # print(f'search:"{search}"')
            if re.search(search, sp_output, re.MULTILINE):
                # print(f'Found vendor id that matches')
                devices = get_devices_with_vendor_id(vid)
                for device in devices:
                    possible_devices.add(device)
    return possible_devices


def detect_windows_needs_driver(sd, print_reason=False) -> bool:
    """
    Determine whether a Windows driver must be installed for the given supported device.

    Parameters
    ----------
        sd (object | None): Supported device object (or None). If provided, its
            `usb_vendor_id_in_hex` attribute is used to look up matching PnP devices.
        print_reason (bool): If True and a driver-install failure is detected, print
            the detailed PowerShell output explaining the failure.

    Returns
    -------
        bool: `true` if Windows indicates the device has a failed installation and
            a driver likely needs to be installed, `false` otherwise.

    """
    need_to_install_driver: bool = False

    if sd:
        system = platform.system()
        # print(f'in detect_windows_needs_driver system:{system}')

        if system == "Windows":
            # if windows, see if we can find a DeviceId with the vendor id
            # Get-PnpDevice  | Where-Object{ ($_.DeviceId -like '*10C4*')} | Format-List
            command = 'powershell.exe "[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8; Get-PnpDevice | Where-Object{ ($_.DeviceId -like '
            command += f"'*{sd.usb_vendor_id_in_hex.upper()}*'"
            command += ')} | Format-List"'

            # print(f'command:{command}')
            _, sp_output = subprocess.getstatusoutput(command)
            # print(f'sp_output:{sp_output}')
            search = "CM_PROB_FAILED_INSTALL"
            # print(f'search:"{search}"')
            if re.search(search, sp_output, re.MULTILINE):
                need_to_install_driver = True
                # if the want to see the reason
                if print_reason:
                    logger.debug(sp_output)
    return need_to_install_driver


def eliminate_duplicate_port(ports: List) -> List:
    """
    Reduce paired serial port paths to a single representative when they likely refer to the same physical device.

    This function examines a list of serial port path strings and, when the list contains exactly two entries
    that match known duplicate naming patterns (e.g., usbserial vs wchusbserial, usbmodem vs wchusbserial,
    SLAB_USBtoUART vs usbserial), returns a list containing a single preferred port.
    If no duplicate pattern is detected or the list length is not two, the original list is returned unchanged.

    Parameters
    ----------
        ports (List[str]): A list of serial port device path strings.

    Returns
    -------
        List[str]: Either the original list of ports or a list containing one representative port when a duplicate pair is recognized.

    """
    new_ports = []
    if len(ports) != 2:
        new_ports = ports
    else:
        ports.sort()
        if "usbserial" in ports[0] and "wchusbserial" in ports[1]:
            first = ports[0].replace("usbserial-", "")
            second = ports[1].replace("wchusbserial", "")
            if first == second:
                new_ports.append(ports[1])
        elif "usbmodem" in ports[0] and "wchusbserial" in ports[1]:
            first = ports[0].replace("usbmodem", "")
            second = ports[1].replace("wchusbserial", "")
            if first == second:
                new_ports.append(ports[1])
        elif "SLAB_USBtoUART" in ports[0] and "usbserial" in ports[1]:
            new_ports.append(ports[1])
        else:
            new_ports = ports
    return new_ports


def is_windows11() -> bool:
    """
    Determine whether the running OS is Windows 11.

    Returns
    -------
        `true` if the OS is Windows and the system version patch is 22000 or greater, `false` otherwise (including when detection fails).

    """
    is_win11: bool = False
    if platform.system() == "Windows":
        try:
            if float(platform.release()) >= 10.0:
                patch = platform.version().split(".")[2]
                # in case they add some number suffix later, just get first 5 chars of patch
                patch = patch[:5]
                if int(patch) >= 22000:
                    is_win11 = True
        except Exception:
            logger.exception("Problem detecting Windows 11")
    return is_win11


def get_unique_vendor_ids() -> Set[str]:
    """
    Collect unique USB vendor ID strings from the module's supported_devices.

    Returns
    -------
        Set[str]: A set of vendor ID strings in hex form (for example, "0x239A").

    """
    vids = set()
    for d in supported_devices:
        if d.usb_vendor_id_in_hex:
            vids.add(d.usb_vendor_id_in_hex)
    return vids


def get_devices_with_vendor_id(vid: str) -> Set:  # Set[SupportedDevice]
    """
    Return the set of supported devices that match the given USB vendor ID.

    Parameters
    ----------
        vid (str): USB vendor ID as a hex string (for example, "0x239A").

    Returns
    -------
        Set[SupportedDevice]: A set of SupportedDevice entries whose usb_vendor_id_in_hex equals `vid`.

    """
    sd = set()
    for d in supported_devices:
        if d.usb_vendor_id_in_hex == vid:
            sd.add(d)
    return sd


def active_ports_on_supported_devices(sds, eliminate_duplicates=False) -> Set[str]:
    """
    Return the set of active serial port paths for the provided supported devices, resolving ports according to the current operating system.

    Parameters
    ----------
        sds (Iterable): An iterable of SupportedDevice-like objects; each must expose
            platform-specific base port attributes (e.g., baseport_on_linux,
            baseport_on_mac, baseport_on_windows) used to discover matching ports.
        eliminate_duplicates (bool): If True, collapse likely duplicate port entries
            (platform-dependent heuristics) before returning.

    Returns
    -------
        Set[str]: A set of active port path strings (e.g., "/dev/ttyUSB0" or "COM3").

    """
    ports: Set = set()
    baseports: Set = set()
    system: str = platform.system()

    # figure out what possible base ports there are
    for d in sds:
        if system == "Linux":
            baseports.add(d.baseport_on_linux)
        elif system == "Darwin":
            baseports.add(d.baseport_on_mac)
        elif system == "Windows":
            baseports.add(d.baseport_on_windows)

    for bp in baseports:
        if system == "Linux":
            # see if we have any devices (ignoring any stderr output)
            command = f"ls -al /dev/{bp}* 2> /dev/null"
            # print(f'command:{command}')
            _, ls_output = subprocess.getstatusoutput(command)
            # print(f'ls_output:{ls_output}')
            # if we got output, there are ports
            if len(ls_output) > 0:
                # print('got output')
                # for each line of output
                lines = ls_output.split("\n")
                # print(f'lines:{lines}')
                for line in lines:
                    parts = line.split(" ")
                    # print(f'parts:{parts}')
                    port = parts[-1]
                    # print(f'port:{port}')
                    ports.add(port)
        elif system == "Darwin":
            # see if we have any devices (ignoring any stderr output)
            command = f"ls -al /dev/{bp}* 2> /dev/null"
            # print(f'command:{command}')
            _, ls_output = subprocess.getstatusoutput(command)
            # print(f'ls_output:{ls_output}')
            # if we got output, there are ports
            if len(ls_output) > 0:
                # print('got output')
                # for each line of output
                lines = ls_output.split("\n")
                # print(f'lines:{lines}')
                for line in lines:
                    parts = line.split(" ")
                    # print(f'parts:{parts}')
                    port = parts[-1]
                    # print(f'port:{port}')
                    ports.add(port)
        elif system == "Windows":
            # for each device in supported devices found
            for d in sds:
                # find the port(s)
                com_ports = detect_windows_port(d)
                # print(f'com_ports:{com_ports}')
                # add all ports
                for com_port in com_ports:
                    ports.add(com_port)
    if eliminate_duplicates:
        portlist: List = eliminate_duplicate_port(list(ports))
        portlist.sort()
        ports = set(portlist)
    return ports


def detect_windows_port(
    sd: Optional[SupportedDevice],
) -> Set[str]:
    """
    Detect Windows COM ports associated with a supported USB device.

    Searches present PnP devices on Windows for entries containing the device's
    usb_vendor_id_in_hex and returns any discovered COM port identifiers.

    Parameters
    ----------
        sd (Optional[SupportedDevice]): SupportedDevice whose `usb_vendor_id_in_hex`
            will be used to find matching PnP devices. If `None` or if the system
            is not Windows or the vendor id is missing, the function returns an
            empty set.

    Returns
    -------
        Set[str]: A set of COM port names (e.g., "COM3", "COM4") discovered for the
        device; empty if none found.

    """
    ports = set()

    if sd:
        # Type narrowing: sd is not None inside this block
        device = sd
        system = platform.system()
        vendor_id = device.usb_vendor_id_in_hex

        if system == "Windows" and vendor_id is not None:
            command = (
                'powershell.exe "[Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8;'
                "Get-PnpDevice -PresentOnly | Where-Object{ ($_.DeviceId -like "
            )
            command += f"'*{vendor_id.upper()}*'"
            command += ')} | Format-List"'

            # print(f'command:{command}')
            _, sp_output = subprocess.getstatusoutput(command)
            # print(f'sp_output:{sp_output}')
            p = re.compile(r"\(COM(.*)\)")
            for x in p.findall(sp_output):
                # print(f'x:{x}')
                ports.add(f"COM{x}")
    return ports


def check_if_newer_version() -> Optional[str]:
    """
    Check PyPI for a newer Meshtastic release than the active installation.

    Attempts to fetch the package metadata from PyPI and compares the latest PyPI version
    to the currently active version. Returns the PyPI version string when it is newer;
    returns None if no newer version is available or if the check fails.

    Returns
    -------
        pypi_version (Optional[str]): The newer PyPI version string if available, `None` otherwise.

    """
    pypi_version: Optional[str] = None
    try:
        url: str = "https://pypi.org/pypi/meshtastic/json"
        data = requests.get(url, timeout=5).json()
        pypi_version = data["info"]["version"]
    except Exception:
        pass
    act_version = get_active_version()

    if pypi_version is None:
        return None
    try:
        parsed_act_version = pkg_version.parse(act_version)
        parsed_pypi_version = pkg_version.parse(pypi_version)
        # Note: if handed "None" when we can't download the pypi_version,
        # this gets a TypeError:
        # "TypeError: expected string or bytes-like object, got 'NoneType'"
        # Handle that below?
    except pkg_version.InvalidVersion:
        return pypi_version

    if parsed_pypi_version <= parsed_act_version:
        return None

    return pypi_version


def message_to_json(message: Message, multiline: bool = False) -> str:
    """
    Serialize a protobuf Message to JSON while including fields that have no presence.

    Parameters
    ----------
        message (Message): The protobuf message to serialize.
        multiline (bool): Preserve multi-line formatting when True; use compact
            single-line JSON when False.

    Returns
    -------
        str: JSON string representation of the message.

    """
    try:
        json_str = MessageToJson(
            message,
            always_print_fields_with_no_presence=True,
            indent=2 if multiline else None,
        )
    except TypeError:
        json_str = MessageToJson(  # type: ignore[call-arg]  # pylint: disable=E1123
            message,
            # pyright: ignore[reportCallIssue]  # Older protobuf uses including_default_value_fields
            including_default_value_fields=True,  # pyright: ignore[reportCallIssue]
            indent=2 if multiline else None,
        )
    return json_str


def messageToJson(message: Message, multiline: bool = False) -> str:
    """
    Serialize a protobuf Message to a JSON string.

    Parameters
    ----------
        message (Message): The protobuf Message to serialize.
        multiline (bool): If True, preserve multiline formatting; if False, emit
            compact single-line JSON.

    Returns
    -------
        json_str (str): JSON representation of the message.

    """
    return message_to_json(message, multiline=multiline)


def to_node_num(node_id: Union[int, str]) -> int:
    """
    Convert a node identifier in various textual forms to its integer node number.

    Accepts an int or a string in any of these forms:
    - decimal (e.g., "42")
    - hexadecimal with "0x" prefix (e.g., "0x2A")
    - hexadecimal without "0x" (e.g., "2A")
    - optional leading "!" (e.g., "!0x2A" or "!42")

    Parameters
    ----------
        node_id (int | str): The node identifier to normalize.

    Returns
    -------
        int: The parsed integer node number.

    """
    if isinstance(node_id, int):
        return node_id
    s = str(node_id).strip()
    if s.startswith("!"):
        s = s[1:]
    if s.lower().startswith("0x"):
        return int(s, 16)
    try:
        return int(s, 10)
    except ValueError:
        return int(s, 16)


def flags_to_list(flag_type: Any, flags: int) -> List[str]:
    """
    Convert a protobuf enum bitfield into a list of active flag names.

    Parameters
    ----------
        flag_type (Any): Protobuf EnumTypeWrapper providing `.keys()` and `.Value(name)` for enum members.
        flags (int): Integer bitfield containing combined enum flag values.

    Returns
    -------
        List[str]: Ordered list of enum member names present in `flags`. If any bits remain that do not match known members,
        a single string of the form `UNKNOWN_ADDITIONAL_FLAGS(<remaining>)` is appended.

    """
    ret = []
    for key in flag_type.keys():
        if key == "EXCLUDED_NONE":
            continue
        if flags & flag_type.Value(key):
            ret.append(key)
            flags = flags - flag_type.Value(key)
    if flags > 0:
        ret.append(f"UNKNOWN_ADDITIONAL_FLAGS({flags})")
    return ret
