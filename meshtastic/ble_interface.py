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
        client = BLEClient(device.address, disconnected_callback=lambda _: self.close())
        client.connect()
        client.discover()
        return client

    def _receiveFromRadioImpl(self) -> None:
        while self._want_receive:
            if self.should_read:
                self.should_read = False
                retries: int = 0
                while self._want_receive:
                    if self.client is None:
                        logging.debug(f"BLE client is None in _receiveFromRadioImpl, attempting to stop.")
                        self._want_receive = False # Signal loop to stop
                        # If not already shutting down, this implies an unexpected state.
                        # Trigger _disconnected to allow higher layers to react (e.g., reconnect).
                        if not self._shutdown_flag and not self._disconnection_sent:
                            logging.warning("BLE client became None unexpectedly. Triggering disconnect.")
                            self._disconnection_sent = True
                            self._disconnected()
                        continue # Exit this iteration, loop condition will handle termination

                    try:
                        # Check if client or its bleak_client is available before read
                        if not self.client or not self.client.bleak_client:
                            logging.warning("self.client or self.client.bleak_client is None before GATT read. Stopping receive loop.")
                            self._want_receive = False
                            if not self._shutdown_flag and not self._disconnection_sent:
                                self._disconnection_sent = True
                                self._disconnected()
                            continue

                        b = bytes(self.client.read_gatt_char(FROMRADIO_UUID))
                    except BleakDBusError as e:
                        logging.warning(f"BleakDBusError in _receiveFromRadioImpl: {e}. Assuming disconnection.")
                        self._want_receive = False # Signal loop to stop
                        if not self._shutdown_flag and not self._disconnection_sent:
                            self._disconnection_sent = True
                            self._disconnected()
                        continue # Ensure we process the _want_receive = False
                    except BleakError as e:
                        # Check for common disconnection messages
                        if "Not connected" in str(e) or "disconnected" in str(e).lower() or "not available" in str(e).lower():
                            logging.warning(f"BleakError (likely disconnect) in _receiveFromRadioImpl: {e}")
                            self._want_receive = False # Signal loop to stop
                            if not self._shutdown_flag and not self._disconnection_sent:
                                self._disconnection_sent = True
                                self._disconnected()
                            continue # Ensure we process the _want_receive = False
                        else:
                            # For other BleakErrors, log as error and potentially stop, or retry?
                            # For now, treat as fatal for this read attempt, but let loop continue unless _want_receive is set.
                            logging.error(f"Unhandled BleakError in _receiveFromRadioImpl: {e}")
                            # Depending on severity, you might want to set self._want_receive = False here too.
                            # For now, let it retry if it was a transient read issue not a disconnect.
                            # However, most BleakErrors during read_gatt_char are serious.
                            # Let's be safe and assume it's a disconnect type of error.
                            self._want_receive = False # Signal loop to stop
                            if not self._shutdown_flag and not self._disconnection_sent:
                                self._disconnection_sent = True
                                self._disconnected()
                            continue
                    except RuntimeError as e_runtime: # Catch runtime errors from async_await if client is shutting down
                        logging.warning(f"RuntimeError in _receiveFromRadioImpl (possibly client shutdown): {e_runtime}")
                        self._want_receive = False # Signal loop to stop
                        if not self._shutdown_flag and not self._disconnection_sent:
                            self._disconnection_sent = True
                            self._disconnected()
                        continue
                    except Exception as e_generic: # Catch any other unexpected error
                        logging.error(f"Unexpected exception in _receiveFromRadioImpl: {e_generic}", exc_info=True)
                        self._want_receive = False # Signal loop to stop as a precaution
                        if not self._shutdown_flag and not self._disconnection_sent:
                            self._disconnection_sent = True
                            self._disconnected()
                        continue

                    if not b: # Empty read
                        if retries < 5:
                            #logging.debug("Empty read, retrying...")
                            time.sleep(0.1) # Brief pause before retry
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
            logging.debug("BLEInterface.close already called or in progress.")
            return
        self._shutdown_flag = True
        logging.info("BLEInterface.close initiated.")

        # 1. Stop the _receiveThread
        logging.debug("Stopping _receiveThread...")
        self._want_receive = False  # Signal the thread to stop
        if self._receiveThread and self._receiveThread.is_alive():
            if threading.current_thread() == self._receiveThread:
                logging.warning("BLEInterface.close() called from _receiveThread. Cannot join self.")
            else:
                self._receiveThread.join(timeout=5.0)  # Increased timeout
                if self._receiveThread.is_alive():
                    logging.warning("_receiveThread did not shut down cleanly after 5s.")
                else:
                    logging.debug("_receiveThread joined successfully.")
        self._receiveThread = None # Ensure it's cleared

        # 2. Unregister atexit handler
        if hasattr(self, '_exit_handler') and self._exit_handler:
            logging.debug("Unregistering atexit handler.")
            try:
                atexit.unregister(self._exit_handler)
            except Exception as e_atexit: # More generic catch as atexit errors can be varied
                logging.warning(f"Error unregistering atexit handler: {e_atexit}")
            self._exit_handler = None


        # 3. Disconnect and close the BLEClient (self.client which is a BLEClient instance)
        ble_client_to_close = self.client
        self.client = None # Set to None early to prevent reuse during/after close

        if ble_client_to_close:
            logging.debug("Closing BLEClient (self.client)...")
            try:
                # The BLEClient.disconnect() is async and managed by BLEClient.close()
                # BLEClient.close() handles BleakClient's async disconnect and internal loop.
                ble_client_to_close.close() # This should handle everything for the BLEClient part
                logging.debug("BLEClient (self.client) closed.")
            except Exception as e:
                logging.error(f"Error during BLEClient (self.client) close: {e}")
        else:
            logging.debug("No active BLEClient (self.client) to close.")

        # 4. Call MeshInterface.close(self) for base class cleanup
        try:
            logging.debug("Calling MeshInterface.close(self).")
            MeshInterface.close(self)
        except Exception as e:
            logging.error(f"Error closing mesh interface (superclass): {e}")

        # 5. Send the _disconnected() signal if not already sent
        # This should happen after all resources are supposedly released.
        if not self._disconnection_sent:
            logging.debug("Sending _disconnected signal.")
            self._disconnection_sent = True # Set before calling to prevent re-entry if _disconnected calls close
            self._disconnected()
        else:
            logging.debug("_disconnected signal already sent or not needed.")

        logging.info("BLEInterface.close finished.")


class BLEClient:
    """Client for managing connection to a BLE device"""

    def __init__(self, address=None, **kwargs) -> None:
        self._eventLoop = asyncio.new_event_loop()
        self._eventThread = Thread(
            target=self._run_event_loop, name="BLEClient", daemon=True
        )
        self._eventThread.start()
        self._shutdown_flag = False
        self._bleak_client_lock = threading.Lock()  # Lock for accessing/modifying self.bleak_client
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
            logging.debug("BLEClient close already called.")
            return
        self._shutdown_flag = True
        logging.debug("BLEClient close initiated.")

        # Stop accepting new tasks
        # Cancel all pending futures
        # Needs to be done before stopping the loop
        with self._pending_tasks_lock:
            tasks_to_cancel = list(self._pending_tasks)
            self._pending_tasks.clear()
            logging.debug(f"Cancelling {len(tasks_to_cancel)} pending tasks.")
            for future in tasks_to_cancel:
                if not future.done():
                    try:
                        future.cancel()
                    except Exception as e_cancel:
                        logging.warning(f"Exception while cancelling future: {e_cancel}")

        # Schedule the event loop shutdown
        if self._eventLoop and self._eventLoop.is_running():
            logging.debug("Requesting event loop to stop.")
            self._eventLoop.call_soon_threadsafe(self._eventLoop.stop)
        else:
            logging.debug("Event loop not running or already closed.")

        # Wait for event thread to finish, but not if we are the event thread
        if self._eventThread and self._eventThread.is_alive() and threading.current_thread() != self._eventThread:
            logging.debug("Joining event thread.")
            self._eventThread.join(timeout=10.0)  # Increased timeout
            if self._eventThread.is_alive():
                logging.warning("BLE event thread did not shut down cleanly after 10s.")
            else:
                logging.debug("BLE event thread joined successfully.")
        elif threading.current_thread() == self._eventThread:
            logging.debug("Close called from event thread, not joining self.")
        elif not self._eventThread or not self._eventThread.is_alive():
            logging.debug("Event thread already stopped or not started.")

        # Release BleakClient resources if it exists
        # This should ideally be done by the BleakClient's __aexit__ or similar
        # but we ensure it's None to prevent further use.
        with self._bleak_client_lock:
            if self.bleak_client:
                # Note: BleakClient's disconnect is async, which we can't easily call here
                # if the loop is already stopped. Relies on Bleak's internal cleanup.
                logging.debug("Setting bleak_client to None.")
                self.bleak_client = None # Make sure we don't try to use it again

        logging.debug("BLEClient close finished.")

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def async_await(self, coro, timeout=30.0):  # pylint: disable=C0116
        """Execute async coroutine with default 30 second timeout"""
        if self._shutdown_flag:
            logging.warning("BLEClient.async_await called during shutdown.")
            raise RuntimeError("BLE client is shutting down or already shut down.")

        if not self._eventLoop or not self._eventLoop.is_running():
            logging.error("BLEClient.async_await called but event loop is not running.")
            raise RuntimeError("BLEClient event loop is not running.")

        future = self.async_run(coro)
        if not future: # async_run might return None if loop is not active
            raise RuntimeError("Failed to schedule coroutine, event loop might be closing.")

        with self._pending_tasks_lock:
            # Re-check shutdown flag inside lock for thread safety
            if self._shutdown_flag:
                logging.warning("BLEClient.async_await detected shutdown after future creation.")
                if not future.done(): # Check if future is already done (e.g. cancelled by another thread)
                    future.cancel()
                # Do not add to _pending_tasks if we are shutting down
                # Raising an error here is important to signal the caller
                raise RuntimeError("BLE client is shutting down during task submission.")
            self._pending_tasks.add(future)

        try:
            return future.result(timeout)
        except asyncio.CancelledError:
            logging.debug("Async task cancelled in async_await.")
            raise # Re-raise CancelledError so caller knows
        except asyncio.TimeoutError:
            logging.warning(f"Async task timed out after {timeout}s in async_await.")
            if not future.done():
                future.cancel() # Attempt to cancel on timeout
            raise # Re-raise TimeoutError
        except Exception as e:
            logging.error(f"Exception in async_await: {e}")
            if not future.done():
                future.cancel() # Attempt to cancel on other exceptions
            raise
        finally:
            with self._pending_tasks_lock:
                self._pending_tasks.discard(future) # Ensure removal

    def async_run(self, coro):  # pylint: disable=C0116
        if self._shutdown_flag or not self._eventLoop or not self._eventLoop.is_running():
            logging.warning("BLEClient.async_run called during shutdown or loop not running.")
            return None # Indicate failure to schedule
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)
        except RuntimeError as e: # Can happen if loop is closing
            logging.error(f"RuntimeError in async_run (loop might be closing): {e}")
            return None


    def _run_event_loop(self):
        try:
            asyncio.set_event_loop(self._eventLoop)
            self._eventLoop.run_forever()
        except Exception as e:
            logging.error(f"Error in BLE event loop: {e}")
        finally:
            try:
                # Attempt to cancel all remaining tasks in the loop.
                # This is a best-effort cleanup.
                if self._eventLoop.is_running(): # Check if loop is still running before gathering tasks
                    pending = asyncio.all_tasks(self._eventLoop)
                    if pending:
                        logging.debug(f"Event loop stopping, cancelling {len(pending)} pending tasks.")
                        for task in pending:
                            if not task.done():
                                task.cancel()
                        try:
                            # Allow tasks to process cancellation
                            self._eventLoop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                            logging.debug("Pending tasks gathered/cancelled in event loop shutdown.")
                        except RuntimeError as e_gather: # Loop might already be stopped
                             logging.warning(f"RuntimeError during gather in event loop shutdown: {e_gather}")
                        except Exception as e_gather_generic:
                             logging.error(f"Generic error during gather in event loop shutdown: {e_gather_generic}")
                else:
                    logging.debug("Event loop already stopped, no tasks to cancel via all_tasks.")
            except Exception as e:
                logging.error(f"Error during final task cancellation in _run_event_loop: {e}")
            finally:
                if not self._eventLoop.is_closed():
                    logging.debug("Closing event loop.")
                    self._eventLoop.close()
                else:
                    logging.debug("Event loop was already closed.")
