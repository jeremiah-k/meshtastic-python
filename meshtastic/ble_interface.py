"""Bluetooth interface
"""
import asyncio
import atexit
import logging
import struct
import threading
import time
import io
import concurrent.futures # Added
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
        self._read_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='BLEGATTRead'
        ) # Added

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
        client = BLEClient(device.address, disconnected_callback=self._on_disconnected)
        client.connect()
        client.discover()
        return client

    def _on_disconnected(self, client):
        """
        Handle BLE disconnection callback safely by scheduling close() in a separate thread.
        This prevents deadlocks when the callback is triggered from within Bleak's async context.
        """
        try:
            logging.debug("BLE disconnected callback triggered, scheduling close() in separate thread")
            # Schedule close() in a separate thread to avoid blocking the callback
            import threading
            close_thread = threading.Thread(target=self.close, name="BLEDisconnectClose", daemon=True)
            close_thread.start()
        except Exception as e:
            logging.error(f"Error in BLE disconnect callback: {e}")

    def _receiveFromRadioImpl(self) -> None:
        while self._want_receive:
            if self.should_read:
                self.should_read = False
                retries: int = 0
                while self._want_receive:
                    if self.client is None:
                        logging.debug(f"BLE client is None, shutting down")
                        self._want_receive = False
                        if not self._shutdown_flag and not self._disconnection_sent:
                            self._disconnection_sent = True
                            self._disconnected()
                        continue # continue to top of while self._want_receive loop to check flag

                    b = b"" # Ensure b is initialized
                    # Pre-read check for client status
                    if self.client is None or self.client._shutdown_flag:
                        logging.debug("BLEClient is None or shutting down (pre-read check), exiting _receiveFromRadioImpl inner loop.")
                        self._want_receive = False # Ensure outer loop also terminates
                        break # Exit inner while self._want_receive loop

                    try:
                        # Execute read_gatt_char in a separate thread to make it truly non-blocking
                        # for the _receiveThread's control flow.
                        # The timeout for read_gatt_char itself is 1.0s (passed to async_await).
                        # The future.result() timeout is slightly longer.
                        read_future = self._read_executor.submit(
                            self.client.read_gatt_char, FROMRADIO_UUID, timeout=1.0
                        )
                        b = bytes(read_future.result(timeout=1.1)) # Timeout for future.result()
                    except concurrent.futures.TimeoutError:
                        b = b"" # Treat timeout from future.result() as no data
                        logging.debug("ThreadPoolExecutor: Timeout waiting for read_gatt_char result.")
                        # The task in the executor will continue running until its own timeout (1.0s for read_gatt_char)
                        # We don't explicitly cancel read_future here as it's complex to manage with bleak's async nature.
                        # The goal is to unblock _receiveThread.
                    except asyncio.TimeoutError: # Should ideally be caught by the ThreadPoolExecutor's timeout
                        b = b""
                        logging.debug("Asyncio TimeoutError somehow propagated from read_gatt_char through executor.")
                    except BleakDBusError as e:
                        logging.debug(f"Device disconnected (DBus) during read_gatt_char via executor: {e}")
                        self._want_receive = False
                        if not self._shutdown_flag and not self._disconnection_sent:
                            self._disconnection_sent = True
                            self._disconnected()
                        continue
                    except BleakError as e:
                        if "Not connected" in str(e) or "not connected" in str(e).lower():
                            logging.debug(f"Device disconnected (BleakError) during read_gatt_char via executor: {e}")
                            self._want_receive = False
                            if not self._shutdown_flag and not self._disconnection_sent:
                                self._disconnection_sent = True
                                self._disconnected()
                            continue
                        else:
                            logging.error(f"Unhandled BleakError from read_gatt_char via executor: {e}")
                            time.sleep(0.1)
                            continue
                    except Exception as e:
                        logging.error(f"Unexpected error from read_gatt_char via executor: {e}", exc_info=True)
                        self._want_receive = False
                        if not self._shutdown_flag and not self._disconnection_sent:
                            self._disconnection_sent = True
                            self._disconnected()
                        continue

                    if not b: # No data received
                        if retries < 5 and self._want_receive:
                            time.sleep(0.1)
                            retries += 1
                            continue
                        break
                    logging.debug(f"FROMRADIO read: {b.hex()}")
                    self._handleFromRadio(b)

                # After potential read attempt and error handling, check if we should still be running
                # or if the client has been shut down.
                if not self._want_receive: # Primary check for this thread
                    logging.debug("_want_receive is false, exiting _receiveFromRadioImpl inner loop.")
                    break
                if self.client is None or self.client._shutdown_flag:
                    logging.debug("BLEClient is None or client is shutting down, exiting _receiveFromRadioImpl.")
                    self._want_receive = False # Ensure outer loop also terminates
                    break
            else: # This 'else' corresponds to 'if self.should_read:'
                time.sleep(0.01)

            # Check before looping back in the main 'while self._want_receive:'
            if self.client is None or self.client._shutdown_flag:
                logging.debug("BLEClient is None or client is shutting down, stopping _receiveFromRadioImpl outer loop.")
                self._want_receive = False # Ensure outer loop also terminates

    def _sendToRadioImpl(self, toRadio) -> None:
        # CRITICAL DEBUG: Add immediate logging
        print(f"_sendToRadioImpl CALLED - shutdown_flag: {self._shutdown_flag}")
        logging.debug(f"_sendToRadioImpl CALLED - shutdown_flag: {self._shutdown_flag}")

        # Don't send data if we're shutting down
        if self._shutdown_flag:
            logging.debug("Ignoring TORADIO write during shutdown")
            return

        b: bytes = toRadio.SerializeToString()
        if b and self.client:  # we silently ignore writes while we are shutting down
            # Multiple checks to prevent hanging on disconnected client
            if hasattr(self.client, '_shutdown_flag') and self.client._shutdown_flag:
                logging.debug("Ignoring TORADIO write - BLE client is shutting down")
                return

            # Check if the underlying bleak client is still connected
            try:
                if hasattr(self.client, 'bleak_client') and self.client.bleak_client:
                    if not self.client.bleak_client.is_connected:
                        logging.debug("Ignoring TORADIO write - BLE device is not connected")
                        return
            except Exception as e:
                logging.debug(f"Error checking BLE connection status: {e}")
                return

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
        # CRITICAL DEBUG: Add immediate logging to see if close() is called at all
        print("BLEInterface.close() CALLED - immediate debug print")
        logging.debug("BLEInterface.close() CALLED - immediate debug log")

        # Attempt to unregister atexit handler first to prevent conflicts
        # especially if this close() is part of a manual shutdown sequence.
        if hasattr(self, '_exit_handler') and self._exit_handler:
            try:
                logging.debug("Unregistering atexit handler in BLEInterface.close()...")
                atexit.unregister(self._exit_handler)
                logging.debug("atexit handler unregistered successfully.")
                self._exit_handler = None # Clear it so we don't try again
            except ValueError: # Can happen if already unregistered or never properly registered
                logging.debug("atexit handler was already unregistered or not found.")
            except Exception as e:
                logging.error(f"Error unregistering atexit handler: {e}")

        # Prevent multiple close attempts
        if self._shutdown_flag:
            logging.debug("BLEInterface.close() called again, but shutdown already in progress.")
            return
        self._shutdown_flag = True
        logging.debug("BLEInterface.close() started, shutdown_flag set.")

        try:
            logging.debug("Calling MeshInterface.close() (super().close())")
            MeshInterface.close(self)
            logging.debug("MeshInterface.close() completed.")
        except Exception as e:
            logging.error(f"Error closing mesh interface (super().close()): {e}")

        logging.debug("Setting self._want_receive = False for BLEReceive thread.")
        self._want_receive = False  # Signal the reader thread to stop

        if self.client:
            logging.debug("Attempting to disconnect BLE client (self.client.disconnect(timeout=5.0))...")
            try:
                self.client.disconnect(timeout=5.0) # Disconnect before joining thread
                logging.debug("BLE client disconnect call completed.")
            except asyncio.TimeoutError:
                logging.warning("Timeout during BLE client disconnect operation.")
            except RuntimeError as e: # Catch runtime errors from async_await if client is shutting down
                logging.warning(f"RuntimeError during BLE client disconnect: {e}")
            except Exception as e:
                logging.error(f"Error during BLE client disconnect: {e}")
        else:
            logging.debug("self.client is None, skipping client disconnect call.")

        if self._receiveThread and self._receiveThread.is_alive():
            logging.debug(f"Joining BLEReceive thread (timeout=5.0)...")
            self._receiveThread.join(timeout=5.0)
            if self._receiveThread.is_alive():
                logging.warning("BLEReceive thread did not exit cleanly after 5 seconds.")
            else:
                logging.debug("BLEReceive thread joined successfully.")
            self._receiveThread = None
        elif self._receiveThread:
            logging.debug("BLEReceive thread was not alive before join attempt.")
            self._receiveThread = None # Clear it if it existed but wasn't alive
        else:
            logging.debug("No BLEReceive thread to join.")

        # Shutdown the ThreadPoolExecutor
        if hasattr(self, '_read_executor') and self._read_executor:
            logging.debug("Shutting down BLEGATTRead executor...")
            try:
                # Don't wait for pending futures as they may be hanging on disconnected BLE operations
                # The _receiveThread has already been stopped, so no new tasks will be submitted
                self._read_executor.shutdown(wait=False, cancel_futures=True)
                logging.debug("BLEGATTRead executor shutdown initiated (non-blocking).")
            except Exception as e:
                logging.error(f"Error shutting down BLEGATTRead executor: {e}", exc_info=True)
            self._read_executor = None


        if self.client:
            # The atexit handler should have been unregistered at the start of this method.
            # If it wasn't (e.g., _exit_handler was None), this is a secondary check,
            # though the primary unregister is at the top.
            if hasattr(self, '_exit_handler') and self._exit_handler:
                try:
                    logging.debug("Secondary check: Unregistering atexit handler...")
                    atexit.unregister(self._exit_handler)
                    self._exit_handler = None
                    logging.debug("Secondary atexit unregistration successful.")
                except Exception: # Broadly catch if it fails here, primary attempt was at start
                    logging.debug("Secondary atexit unregistration failed or already done.")

            logging.debug("Closing BLEClient (self.client.close())...")
            try:
                self.client.close()
                logging.debug("BLEClient (self.client.close()) completed.")
            except Exception as e:
                logging.error(f"Error during self.client.close(): {e}")
            finally:
                self.client = None # Ensure client is set to None after close attempt
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

    def disconnect(self, timeout=30.0, **kwargs):  # pylint: disable=C0116
        """Helper to wrap bleak_client.disconnect with async_await and a custom timeout."""
        # Check if bleak_client exists and is connected before trying to disconnect
        if hasattr(self, 'bleak_client') and self.bleak_client and self.bleak_client.is_connected:
            return self.async_await(self.bleak_client.disconnect(**kwargs), timeout=timeout)
        logging.debug("Skipped disconnect: bleak_client not connected or does not exist.")
        return None # Or appropriate return indicating disconnect was not attempted/needed


    def read_gatt_char(self, *args, timeout=30.0, **kwargs):  # pylint: disable=C0116
        """Helper to wrap read_gatt_char with async_await and a custom timeout."""
        return self.async_await(self.bleak_client.read_gatt_char(*args, **kwargs), timeout=timeout)

    def write_gatt_char(self, *args, timeout=30.0, **kwargs):  # pylint: disable=C0116
        """Helper to wrap write_gatt_char with async_await and a custom timeout."""
        return self.async_await(self.bleak_client.write_gatt_char(*args, **kwargs), timeout=timeout)

    def has_characteristic(self, specifier):
        """Check if the connected node supports a specified characteristic."""
        return bool(self.bleak_client.services.get_characteristic(specifier))

    def start_notify(self, *args, **kwargs):  # pylint: disable=C0116
        self.async_await(self.bleak_client.start_notify(*args, **kwargs))

    def close(self):  # pylint: disable=C0116
        # Set shutdown_flag at the very beginning.
        # If already shutting down (e.g. called re-entrantly), just return.
        if self._shutdown_flag:
            logging.debug("BLEClient.close() called again, shutdown already in progress.")
            return
        self._shutdown_flag = True
        logging.debug("BLEClient.close() started, _shutdown_flag set.")

        try:
            logging.debug(f"BLEClient.close(): Cancelling pending tasks. Found {len(self._pending_tasks)} tasks.")
            with self._pending_tasks_lock:
                tasks_to_cancel = list(self._pending_tasks) # Create a copy
                # We don't clear self._pending_tasks here, async_await's finally block does it.
                # Clearing here might cause issues if a task is finishing concurrently.

            cancelled_count = 0
            for future in tasks_to_cancel:
                if not future.done():
                    logging.debug(f"BLEClient.close(): Cancelling future: {future}")
                    future.cancel()
                    cancelled_count += 1
            logging.debug(f"BLEClient.close(): Requested cancellation for {cancelled_count} tasks.")

            if self._eventLoop and not self._eventLoop.is_closed():
                logging.debug("BLEClient.close(): Scheduling event loop stop.")
                self._eventLoop.call_soon_threadsafe(self._eventLoop.stop)
                logging.debug("BLEClient.close(): Event loop stop scheduled.")
            else:
                logging.warning("BLEClient.close(): Event loop not found, already closed, or None.")

            logging.debug(f"BLEClient.close(): Joining event thread '{self._eventThread.name}' (timeout=5.0s)...")
            self._eventThread.join(timeout=5.0)
            if self._eventThread.is_alive():
                logging.warning(f"BLEClient.close(): Event thread '{self._eventThread.name}' did not shut down cleanly after 5 seconds.")
            else:
                logging.debug(f"BLEClient.close(): Event thread '{self._eventThread.name}' joined successfully.")
        except Exception as e:
            logging.error(f"Error during BLEClient.close() shutdown sequence: {e}", exc_info=True)

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def async_await(self, coro, timeout=30.0):  # pylint: disable=C0116
        """Execute async coroutine with default 30 second timeout"""
        if self._shutdown_flag:
            raise RuntimeError("BLE client is shutting down")

        future = self.async_run(coro)
        with self._pending_tasks_lock:
            if self._shutdown_flag:
                future.cancel()
                # Don't add to pending tasks if shutting down
                try:
                    return future.result(timeout)
                except Exception:
                    raise
            self._pending_tasks.add(future)
        try:
            return future.result(timeout)
        except asyncio.CancelledError:
            # This occurs if the task was cancelled externally (e.g., by BLEClient.close())
            # or if the coroutine itself raised CancelledError.
            logging.warning(f"Task was cancelled (coro: {coro}), possibly due to shutdown.")
            raise # Re-raise to signal cancellation to the caller of async_await
        except TimeoutError: # Specifically catch asyncio.TimeoutError from future.result(timeout)
            logging.warning(f"Task timed out (coro: {coro}, timeout: {timeout}s).")
            # It's important to try to cancel the underlying bleak task if our await timed out
            if not future.done():
                future.cancel()
            raise # Re-raise TimeoutError
        except Exception as e:
            # For any other exception from future.result() or the coroutine itself
            logging.error(f"Exception in awaited task (coro: {coro}): {e}")
            if not future.done(): # Should be done if exception occurred, but good practice
                future.cancel()
            raise # Re-raise the original exception
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
                # Cancel all pending tasks
                pending = asyncio.all_tasks(self._eventLoop)
                for task in pending:
                    task.cancel()

                # Wait for tasks to complete cancellation with timeout
                if pending:
                    try:
                        self._eventLoop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*pending, return_exceptions=True),
                                timeout=2.0
                            )
                        )
                    except asyncio.TimeoutError:
                        logging.warning(f"Timeout waiting for {len(pending)} tasks to cancel during BLE event loop shutdown")
            except Exception as e:
                logging.error(f"Error cancelling tasks: {e}")
            finally:
                self._eventLoop.close()
