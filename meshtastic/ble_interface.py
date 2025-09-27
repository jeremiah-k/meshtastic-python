"""Bluetooth interface
"""
import asyncio
import atexit
import logging
import struct
import io
import threading
from typing import List, Optional, Callable

import google.protobuf
from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakError

from meshtastic.mesh_interface import MeshInterface

from .protobuf import mesh_pb2

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"
logger = logging.getLogger(__name__)


class BLEInterface(MeshInterface):
    """MeshInterface using BLE to connect to devices."""

    class BLEError(Exception):
        """An exception class for BLE errors."""

    def __init__(
        self,
        address: Optional[str] = None,
        noProto: bool = False,
        debugOut: Optional[io.TextIOWrapper] = None,
        noNodes: bool = False,
    ) -> None:
        """Constructor"""
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes
        )
        self.client: Optional[BleakClient] = None
        self._exit_handler: Optional[Callable] = None
        self._read_lock = asyncio.Lock()
        self._event_loop = asyncio.new_event_loop()
        self._event_thread = threading.Thread(
            target=self._run_event_loop, name="BLEEventLoop", daemon=True
        )
        self._event_thread.start()

        if address:
            self.connect(address)

    def _run_event_loop(self):
        """Run the asyncio event loop in this thread."""
        asyncio.set_event_loop(self._event_loop)
        try:
            self._event_loop.run_forever()
        finally:
            self._event_loop.close()

    def _run_coro(self, coro, timeout=None):
        """Run a coroutine on the event loop and wait for the result."""
        if not self._event_loop or not self._event_loop.is_running():
            logger.warning("Event loop not running, can't run coroutine")
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._event_loop)
        return future.result(timeout)

    def __repr__(self):
        rep = f"BLEInterface(address={self.client.address if self.client else None!r}"
        if self.debugOut is not None:
            rep += f", debugOut={self.debugOut!r}"
        if self.noProto:
            rep += ", noProto=True"
        if self.noNodes:
            rep += ", noNodes=True"
        rep += ")"
        return rep

    def from_num_handler(self, _, b: bytes) -> None:
        """Handle callbacks for fromnum notify."""
        from_num = struct.unpack("<I", bytes(b))[0]
        logger.debug(f"FROMNUM notify: {from_num}")
        self._event_loop.create_task(self._receiveFromRadioImpl())

    async def log_radio_handler(self, _, b):  # pylint: disable=C0116
        log_record = mesh_pb2.LogRecord()
        try:
            log_record.ParseFromString(bytes(b))

            message = (
                f"[{log_record.source}] {log_record.message}"
                if log_record.source
                else log_record.message
            )
            self._handleLogLine(message)
        except google.protobuf.message.DecodeError:
            logger.warning("Malformed LogRecord received. Skipping.")

    async def legacy_log_radio_handler(self, _, b):  # pylint: disable=C0116
        log_radio = b.decode("utf-8").replace("\n", "")
        self._handleLogLine(log_radio)

    @staticmethod
    async def _scan_async() -> List[BLEDevice]:
        """Scan for available BLE devices."""
        logger.info("Scanning for BLE devices...")
        return await BleakScanner.discover(service_uuids=[SERVICE_UUID])

    @staticmethod
    def scan() -> List[BLEDevice]:
        """Scan for available BLE devices.
        This method is synchronous and should only be used by the CLI.
        """
        return asyncio.run(BLEInterface._scan_async())

    def connect(self, address: Optional[str], timeout: int = 20) -> None:
        """Connect to a device by address."""

        async def _connect_async():
            logger.info(f"Connecting to BLE device: {address}")

            # Scan for devices
            try:
                devices = await asyncio.wait_for(self._scan_async(), timeout=15)
            except asyncio.TimeoutError as e:
                raise BLEInterface.BLEError("Timed out scanning for BLE devices") from e

            logger.debug(f"Found {len(devices)} BLE devices")

            # Find the specific device
            if address:
                # Try to find by address first
                addressed_devices = list(
                    filter(lambda x: address.lower() in x.address.lower(), devices)
                )
                if not addressed_devices:
                    # If not found by address, try by name
                    addressed_devices = list(
                        filter(
                            lambda x: x.name and address.lower() in x.name.lower(),
                            devices,
                        )
                    )
            else:
                addressed_devices = devices

            if not addressed_devices:
                raise BLEInterface.BLEError(
                    f"No Meshtastic device found for address '{address}'"
                )

            if len(addressed_devices) > 1:
                logger.warning(
                    f"More than one Meshtastic BLE peripheral with identifier or address '{address}' found. Using the first one."
                )

            device = addressed_devices[0]
            logger.debug(f"Found device: {device.name} at {device.address}")

            # Connect to the device
            self.client = BleakClient(
                device.address, disconnected_callback=lambda _: self._disconnected()
            )
            await self.client.connect(timeout=timeout)
            logger.debug("BLE connected")

            # Start notifications
            if self.client.services.get_characteristic(LEGACY_LOGRADIO_UUID):
                logger.debug("Subscribing to LEGACY_LOGRADIO_UUID")
                await self.client.start_notify(
                    LEGACY_LOGRADIO_UUID, self.legacy_log_radio_handler
                )
            if self.client.services.get_characteristic(LOGRADIO_UUID):
                logger.debug("Subscribing to LOGRADIO_UUID")
                await self.client.start_notify(LOGRADIO_UUID, self.log_radio_handler)
            if self.client.services.get_characteristic(FROMNUM_UUID):
                logger.debug("Subscribing to FROMNUM_UUID")
                await self.client.start_notify(FROMNUM_UUID, self.from_num_handler)

        try:
            self._run_coro(_connect_async(), timeout=timeout + 10)

            self._exit_handler = atexit.register(self.close)

            logger.debug("Mesh configure starting")
            self._startConfig()
            if not self.noProto:
                self.waitForConfig()

        except (BleakError, asyncio.TimeoutError, BLEInterface.BLEError) as e:
            logger.error(f"Error connecting to BLE device: {e}", exc_info=True)
            self.close()
            # Re-raise with a more informative message
            raise BLEInterface.BLEError(f"Failed to connect to {address}: {e}") from e

    async def _receiveFromRadioImpl(self) -> None:
        """Receive a packet from the radio"""
        if self.client is None:
            logger.debug("BLE client is None, shutting down")
            return

        async with self._read_lock:
            try:
                # Retry read operation if no data is received
                for i in range(5):
                    b = await self.client.read_gatt_char(FROMRADIO_UUID)
                    if b:
                        logger.debug(f"FROMRADIO read: {b.hex()}")
                        self._handleFromRadio(b)
                        return  # Exit after successful read
                    logger.debug(f"No data received on attempt {i+1}, retrying...")
                    await asyncio.sleep(0.1)
            except BleakError as e:
                logger.error(f"Error reading from BLE: {e}")
                self.close()

    def _sendToRadioImpl(self, toRadio) -> None:
        """Send a packet to the radio"""
        b: bytes = toRadio.SerializeToString()
        if b and self.client:
            logger.debug(f"TORADIO write: {b.hex()}")

            async def _write_async():
                try:
                    await self.client.write_gatt_char(TORADIO_UUID, b, response=True)
                    # After writing, we must read from the device to get the response
                    await self._receiveFromRadioImpl()
                except Exception as e:
                    raise BLEInterface.BLEError(
                        "Error writing BLE (are you in the 'bluetooth' user group? did you enter the pairing PIN on your computer?)"
                    ) from e

            if threading.current_thread() is self._event_thread:
                self._event_loop.create_task(_write_async())
            else:
                self._run_coro(_write_async())

    def _stop_event_loop(self):
        """Stop the event loop"""
        if self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        if self._event_thread:
            self._event_thread.join()

    def close(self) -> None:
        """Close the connection"""
        logger.debug("Closing BLE connection")
        if self._exit_handler:
            atexit.unregister(self._exit_handler)
            self._exit_handler = None

        if self.client and self.client.is_connected:
            self._run_coro(self.client.disconnect())

        self._stop_event_loop()
        self.client = None
        self._disconnected()
