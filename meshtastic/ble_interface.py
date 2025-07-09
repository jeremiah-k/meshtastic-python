"""Bluetooth interface
"""
import asyncio
import atexit
import logging
import struct
import threading
import time
import io
from threading import Thread
from typing import List, Optional

import google.protobuf
from bleak import BleakClient, BleakScanner, BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic.mesh_interface import MeshInterface

from .protobuf import mesh_pb2

SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"
LEGACY_LOGRADIO_UUID = "6c6fd238-78fa-436b-aacf-15c5be1ef2e2"
LOGRADIO_UUID = "5a3d6e49-06e6-4423-9944-e9de8cdf9547"


class BLEInterface(MeshInterface):
    """MeshInterface using BLE to connect to devices."""

    class BLEError(Exception):
        """An exception class for BLE errors."""

    def __init__(
        self,
        address: Optional[str],
        noProto: bool = False,
        debugOut: Optional[io.TextIOWrapper]=None,
        noNodes: bool = False,
    ) -> None:
        MeshInterface.__init__(
            self, debugOut=debugOut, noProto=noProto, noNodes=noNodes
        )

        self.should_read = False
        self._shutdown_flag = False  # Prevent race conditions during shutdown
        self._disconnection_sent = False  # Track if disconnection event was already sent

        logging.debug("Threads starting")
        self._want_receive = True
        self._receiveThread: Optional[Thread] = Thread(
            target=self._receiveFromRadioImpl, name="BLEReceive", daemon=True
        )
        self._receiveThread.start()
        logging.debug("Threads running")

        self.client: Optional[BLEClient] = None
        try:
            logging.debug(f"BLE connecting to: {address if address else 'any'}")
            self.client = self.connect(address)
            logging.debug("BLE connected")
        except BLEInterface.BLEError as e:
            self.close()
            raise e

        if self.client.has_characteristic(LEGACY_LOGRADIO_UUID):
            self.client.start_notify(
                LEGACY_LOGRADIO_UUID, self.legacy_log_radio_handler
            )

        if self.client.has_characteristic(LOGRADIO_UUID):
            self.client.start_notify(LOGRADIO_UUID, self.log_radio_handler)

        logging.debug("Mesh configure starting")
        self._startConfig()
        if not self.noProto:
            self._waitConnected(timeout=60.0)
            self.waitForConfig()

        logging.debug("Register FROMNUM notify callback")
        self.client.start_notify(FROMNUM_UUID, self.from_num_handler)

        # We MUST run atexit (if we can) because otherwise (at least on linux) the BLE device is not disconnected
        # and future connection attempts will fail.  (BlueZ kinda sucks)
        # Note: the on disconnected callback will call our self.close which will make us nicely wait for threads to exit
        self._exit_handler = atexit.register(self.client.disconnect)

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

    def from_num_handler(self, _, b: bytes) -> None:  # pylint: disable=C0116
        """Handle callbacks for fromnum notify.
        Note: this method does not need to be async because it is just setting a bool.
        """
        from_num = struct.unpack("<I", bytes(b))[0]
        logging.debug(f"FROMNUM notify: {from_num}")
        self.should_read = True

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
            logging.warning("Malformed LogRecord received. Skipping.")

    async def legacy_log_radio_handler(self, _, b):  # pylint: disable=C0116
        log_radio = b.decode("utf-8").replace("\n", "")
        self._handleLogLine(log_radio)

    @staticmethod
    def scan() -> List[BLEDevice]:
        """Scan for available BLE devices."""
        with BLEClient() as client:
            logging.info("Scanning for BLE devices (takes 10 seconds)...")
            response = client.discover(
                timeout=10, return_adv=True, service_uuids=[SERVICE_UUID]
            )

            devices = response.values()

            # bleak sometimes returns devices we didn't ask for, so filter the response
            # to only return true meshtastic devices
            # d[0] is the device. d[1] is the advertisement data
            devices = list(
                filter(lambda d: SERVICE_UUID in d[1].service_uuids, devices)
            )
            return list(map(lambda d: d[0], devices))

    def find_device(self, address: Optional[str]) -> BLEDevice:
        """Find a device by address."""

        addressed_devices = BLEInterface.scan()

        if address:
            addressed_devices = list(
                filter(
                    lambda x: address in (x.name, x.address),
                    addressed_devices,
                )
            )

        if len(addressed_devices) == 0:
            raise BLEInterface.BLEError(
                f"No Meshtastic BLE peripheral with identifier or address '{address}' found. Try --ble-scan to find it."
            )
        if len(addressed_devices) > 1:
            raise BLEInterface.BLEError(
                f"More than one Meshtastic BLE peripheral with identifier or address '{address}' found."
            )
        return addressed_devices[0]

    def _sanitize_address(self, address: Optional[str]) -> Optional[str]:  # pylint: disable=E0213
        "Standardize BLE address by removing extraneous characters and lowercasing."
        if address is None:
            return None
        else:
            return address.replace("-", "").replace("_", "").replace(":", "").lower()

    def connect(self, address: Optional[str] = None) -> "BLEClient":
        "Connect to a device by address."

        # Bleak docs recommend always doing a scan before connecting (even if we know addr)
        device = self.find_device(address)
        client = BLEClient(device.address, disconnected_callback=self._on_ble_disconnect)
        client.connect()
        client.discover()
        return client

    def _on_ble_disconnect(self, client):
        """Handle BLE disconnection callback with error protection"""
        try:
            if not self._shutdown_flag:
                logging.debug("BLE disconnected callback triggered")
                self.close()
        except Exception as e:
            logging.error(f"Error in BLE disconnect callback: {e}")

    def _handle_disconnection(self):
        """Handle disconnection safely, avoiding duplicate events."""
        if not (self._shutdown_flag or self._disconnection_sent):
            self._disconnection_sent = True
            self._disconnected()

    def _receiveFromRadioImpl(self) -> None:
        while self._want_receive:
            if self.should_read:
                self.should_read = False
                retries: int = 0
                while self._want_receive:
                    if self.client is None:
                        logging.debug(f"BLE client is None, shutting down")
                        self._want_receive = False
                        continue
                    try:
                        b = bytes(self.client.read_gatt_char(FROMRADIO_UUID))
                    except (BleakDBusError, BleakError) as e:
                        # Device disconnected probably, so end our read loop immediately
                        if isinstance(e, BleakDBusError) or (isinstance(e, BleakError) and "Not connected" in str(e)):
                            logging.debug(f"Device disconnected, shutting down {e}")
                            self._want_receive = False
                            self._handle_disconnection()
                        else:
                            raise BLEInterface.BLEError("Error reading BLE") from e
                    if not b:
                        if retries < 5:
                            time.sleep(0.1)
                            retries += 1
                            continue
                        break
                    logging.debug(f"FROMRADIO read: {b.hex()}")
                    self._handleFromRadio(b)
            else:
                time.sleep(0.01)

    def _sendToRadioImpl(self, toRadio) -> None:
        # Don't send data if we're shutting down
        if self._shutdown_flag:
            logging.debug("Ignoring TORADIO write during shutdown")
            return

        b: bytes = toRadio.SerializeToString()
        if b and self.client:  # we silently ignore writes while we are shutting down
            logging.debug(f"TORADIO write: {b.hex()}")
            try:
                self.client.write_gatt_char(
                    TORADIO_UUID, b, response=True
                )  # FIXME: or False?
                # search Bleak src for org.bluez.Error.InProgress
            except Exception as e:
                if not self._shutdown_flag:  # Only raise error if not shutting down
                    raise BLEInterface.BLEError(
                        "Error writing BLE (are you in the 'bluetooth' user group? did you enter the pairing PIN on your computer?)"
                    ) from e
                else:
                    logging.debug(f"Ignoring BLE write error during shutdown: {e}")
            # Allow to propagate and then make sure we read
            time.sleep(0.01)
            self.should_read = True

    def close(self) -> None:
        # Prevent multiple close attempts
        if self._shutdown_flag:
            return
        self._shutdown_flag = True

        if self._want_receive:
            self._want_receive = False  # Tell the thread we want it to stop
            if self._receiveThread:
                self._receiveThread.join(
                    timeout=2
                )  # If bleak is hung, don't wait for the thread to exit (it is critical we disconnect)
                self._receiveThread = None

        try:
            MeshInterface.close(self)
        except Exception as e:
            logging.error(f"Error closing mesh interface: {e}")

        if self._want_receive:
            self._want_receive = False  # Tell the thread we want it to stop
            if self._receiveThread:
                self._receiveThread.join(
                    timeout=2
                )  # If bleak is hung, don't wait for the thread to exit (it is critical we disconnect)
                self._receiveThread = None

        if self.client:
            try:
                atexit.unregister(self._exit_handler)
            except ValueError:
                # Handler was already unregistered, ignore
                pass

            try:
                # Ensure Bleak client is properly disconnected before closing
                bleak_client = getattr(self.client, 'bleak_client', None)
                if bleak_client:
                    try:
                        # Use async_await with shorter timeout during shutdown
                        awaitable = bleak_client.disconnect()
                        if awaitable:
                            self.client.async_await(awaitable, timeout=5.0)
                    except Exception as e:
                        logging.error(f"Error during Bleak client disconnect: {e}")

                # Ensure the client is closed and resources are released
                self.client.close()

            except Exception as e:
                logging.error(f"Error disconnecting/closing BLE client: {e}")
            finally:
                self.client = None
        # Send disconnection event only if not already sent
        if not self._disconnection_sent:
            self._disconnection_sent = True
            self._disconnected() # send the disconnected indicator up to clients


class BLEClient:
    """Client for managing connection to a BLE device"""

    def __init__(self, address=None, **kwargs) -> None:
        self._eventLoop = asyncio.new_event_loop()
        self._eventThread = Thread(
            target=self._run_event_loop, name="BLEClient", daemon=True
        )
        self._eventThread.start()
        self._shutdown_flag = False
        self._pending_tasks = set()  # Track pending tasks for cleanup
        self._pending_tasks_lock = threading.Lock()

        if not address:
            logging.debug("No address provided - only discover method will work.")
            return

        self.bleak_client = BleakClient(address, **kwargs)

    def discover(self, **kwargs):  # pylint: disable=C0116
        return self.async_await(BleakScanner.discover(**kwargs))

    def pair(self, **kwargs):  # pylint: disable=C0116
        return self.async_await(self.bleak_client.pair(**kwargs))

    def connect(self, **kwargs):  # pylint: disable=C0116
        return self.async_await(self.bleak_client.connect(**kwargs))

    def disconnect(self, **kwargs):  # pylint: disable=C0116
        self.async_await(self.bleak_client.disconnect(**kwargs))

    def read_gatt_char(self, *args, **kwargs):  # pylint: disable=C0116
        return self.async_await(self.bleak_client.read_gatt_char(*args, **kwargs))

    def write_gatt_char(self, *args, **kwargs):  # pylint: disable=C0116
        self.async_await(self.bleak_client.write_gatt_char(*args, **kwargs))

    def has_characteristic(self, specifier):
        """Check if the connected node supports a specified characteristic."""
        return bool(self.bleak_client.services.get_characteristic(specifier))

    def start_notify(self, *args, **kwargs):  # pylint: disable=C0116
        self.async_await(self.bleak_client.start_notify(*args, **kwargs))

    def close(self):  # pylint: disable=C0116
        if self._shutdown_flag:
            return
        self._shutdown_flag = True

        try:
            # Cancel all pending futures first
            with self._pending_tasks_lock:
                tasks_to_cancel = list(self._pending_tasks)
                self._pending_tasks.clear()

            for future in tasks_to_cancel:
                if not future.done():
                    future.cancel()

            # Schedule the event loop shutdown more aggressively
            if self._eventLoop and not self._eventLoop.is_closed():
                try:
                    self._eventLoop.call_soon_threadsafe(self._eventLoop.stop)
                except RuntimeError as e:
                    # Event loop might already be stopped
                    logging.debug(f"Event loop already stopped: {e}")

            # Check if we're in the event thread to avoid self-join
            if threading.current_thread() != self._eventThread:
                # Wait for event thread to finish with shorter timeout
                self._eventThread.join(timeout=3.0)
                if self._eventThread.is_alive():
                    logging.warning("BLE event thread did not shut down cleanly within timeout")
                    # Force stop the event loop if thread is still alive
                    if self._eventLoop and not self._eventLoop.is_closed():
                        try:
                            # Try to force stop the event loop
                            for task in asyncio.all_tasks(self._eventLoop):
                                task.cancel()
                        except Exception as e:
                            logging.debug(f"Error force-cancelling tasks: {e}")
            else:
                # We're in the event thread, can't join ourselves
                # The event loop stop was already scheduled above
                logging.debug("BLE client close() called from event thread, skipping join")
        except Exception as e:
            logging.error(f"Error during BLE client shutdown: {e}")

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def async_await(self, coro, timeout=30.0):  # pylint: disable=C0116
        """Execute async coroutine with default 30 second timeout"""
        if self._shutdown_flag:
            logging.debug("BLE client is shutting down, raising RuntimeError")
            raise RuntimeError("BLE client is shutting down")

        future = self.async_run(coro)

        # Check shutdown flag again after creating future
        if self._shutdown_flag:
            future.cancel()
            raise RuntimeError("BLE client is shutting down")

        with self._pending_tasks_lock:
            if self._shutdown_flag:
                future.cancel()
                raise RuntimeError("BLE client is shutting down")
            self._pending_tasks.add(future)

        try:
            return future.result(timeout)
        except Exception as e:
            # Cancel the future if it's still running
            if not future.done():
                future.cancel()
            # Don't re-raise shutdown-related exceptions during shutdown
            if self._shutdown_flag:
                logging.debug(f"Ignoring expected shutdown error: {e}")
                return None
            raise
        finally:
            with self._pending_tasks_lock:
                self._pending_tasks.discard(future)

    def async_run(self, coro):  # pylint: disable=C0116
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)

    def _run_event_loop(self):
        try:
            asyncio.set_event_loop(self._eventLoop)
            self._eventLoop.run_forever()
        except Exception as e:
            logging.error(f"Error in BLE event loop: {e}")
        finally:
            try:
                # Only try to cancel tasks if the event loop is still running
                if not self._eventLoop.is_closed() and not self._eventLoop.is_running():
                    # Cancel all pending tasks with proper error handling
                    pending = asyncio.all_tasks(self._eventLoop)
                    if pending:
                        for task in pending:
                            if not task.done():
                                task.cancel()

                        # Wait for tasks to complete cancellation with timeout
                        # Only if we have pending tasks and the loop can still run
                        try:
                            self._eventLoop.run_until_complete(
                                asyncio.wait_for(
                                    asyncio.gather(*pending, return_exceptions=True),
                                    timeout=2.0  # Reduced timeout to prevent hanging
                                )
                            )
                        except asyncio.TimeoutError:
                            logging.warning("Timeout waiting for BLE tasks to cancel")
                        except Exception as e:
                            logging.debug(f"Expected error during task cancellation: {e}")
                else:
                    logging.debug("Event loop already closed or running, skipping task cancellation")
            except Exception as e:
                logging.error(f"Error cancelling tasks: {e}")
            finally:
                try:
                    if not self._eventLoop.is_closed():
                        self._eventLoop.close()
                except Exception as e:
                    logging.debug(f"Error closing event loop: {e}")
