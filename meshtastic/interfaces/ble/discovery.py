"""BLE discovery strategies."""
import atexit
import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event, Thread
from typing import List, Optional, TYPE_CHECKING

from bleak import BLEDevice, BleakScanner

from .client import BLEClient
from .config import BLEAK_VERSION, BLEConfig, SERVICE_UUID
from .util import (
    bleak_supports_connected_fallback,
    build_ble_device,
    sanitize_address,
    with_timeout,
)
if TYPE_CHECKING:
    from meshtastic.ble_interface import BLEInterface

logger = logging.getLogger(__name__)


class DiscoveryStrategy(ABC):
    """Abstract base class for device discovery strategies."""

    @abstractmethod
    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Discover BLE devices currently connected to the system, optionally filtered by address.
        
        Attempts to enumerate connected BLE devices via the platform backend and returns devices that advertise the configured service UUID. If the backend does not support connected-device enumeration or an error occurs, an empty list is returned.
        
        Parameters:
            address (Optional[str]): Optional device address or name to filter results; matching is performed against a sanitized form.
            timeout (float): Maximum time in seconds to wait for the backend enumeration.
        
        Returns:
            List[BLEDevice]: A list of discovered BLEDevice objects that advertise the configured service UUID (may be empty).
        """


class ConnectedStrategy(DiscoveryStrategy):
    """Device discovery strategy that enumerates already-connected devices."""

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: _enumerate_connected_devices(address, timeout)
            )
        except Exception as e:  # pragma: no cover  # noqa: BLE001
            logger.debug("Connected device discovery failed: %s", e)
            return []


class DiscoveryManager:
    """Orchestrates scanning + connected-device fallback logic."""

    def __init__(self):
        """
        Initialize the DiscoveryManager and its connected-device fallback strategy.
        
        Creates a single ConnectedStrategy instance assigned to self.connected_strategy for use when scans yield no devices and a connected-device fallback is needed.
        """
        self.connected_strategy = ConnectedStrategy()

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Discover BLE devices advertising the configured service UUID, optionally falling back to already-connected devices when an address is provided.
        
        Performs a BLE scan filtered to SERVICE_UUID and, if no devices are found and an address is supplied, attempts a connected-device enumeration fallback. Any errors during scanning or fallback are caught and result in an empty list or the devices found so far.
        
        Parameters:
            address (Optional[str]): Optional device address or name used to narrow the connected-device fallback; ignored for the initial scan.
        
        Returns:
            List[BLEDevice]: Devices that advertise SERVICE_UUID; returns an empty list if none are found or on error.
        """
        with BLEClient(log_if_no_address=False) as client:
            try:
                logger.debug(
                    "Scanning for BLE devices (takes %.0f seconds)...",
                    BLEConfig.BLE_SCAN_TIMEOUT,
                )
                response = client.discover(
                    timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                    return_adv=True,
                    service_uuids=[SERVICE_UUID],
                )

                devices: List[BLEDevice] = []
                if response is None:
                    logger.warning("BleakScanner.discover returned None")
                elif not isinstance(response, dict):
                    logger.warning(
                        "BleakScanner.discover returned unexpected type: %s",
                        type(response),
                    )
                else:
                    for _, value in response.items():
                        if isinstance(value, tuple):
                            device, adv = value
                        else:
                            logger.warning(
                                "Unexpected return type from BleakScanner.discover: %s",
                                type(value),
                            )
                            continue
                        suuids = getattr(adv, "service_uuids", None)
                        if suuids and SERVICE_UUID in suuids:
                            devices.append(device)

                logger.debug(f"Scan discovered {len(devices)} devices.")

                if not devices and address:
                    logger.debug(
                        "Scan found no devices, trying fallback to already-connected devices..."
                    )
                    fallback_devices = self._discover_connected(address)
                    logger.debug(
                        f"Fallback discovered {len(fallback_devices)} devices."
                    )
                    devices.extend(fallback_devices)

                return devices
            except Exception as e:  # noqa: BLE001 - discovery must never raise
                logger.debug("Device discovery failed: %s", e)
                return []

    def discover_connected_devices(self, address: str) -> List[BLEDevice]:
        """
        Attempt to discover already-connected devices restricted to `address`.
        """
        return _run_coroutine_factory(
            lambda: self.connected_strategy.discover(
                address, BLEConfig.BLE_SCAN_TIMEOUT
            ),
            BLEConfig.BLE_SCAN_TIMEOUT,
            "connected-device fallback",
        )

    def _discover_connected(self, address: str) -> List[BLEDevice]:
        """Internal hook used by tests to exercise connected-device fallback."""
        return self.discover_connected_devices(address)


def _enumerate_connected_devices(
    address: Optional[str], timeout: float
) -> List[BLEDevice]:
    if not bleak_supports_connected_fallback():
        logger.debug(
            "Skipping fallback connected-device scan; bleak %s < %s",
            BLEAK_VERSION,
            ".".join(
                str(part)
                for part in BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
            ),
        )
        return []

    async def _get_devices_async(address_to_find: Optional[str]) -> List[BLEDevice]:
        logger.debug("Starting async connected-device enumeration...")
        scanner = BleakScanner()
        devices_found: List[BLEDevice] = []
        if hasattr(scanner, "_backend") and hasattr(scanner._backend, "get_devices"):
            getter = scanner._backend.get_devices
            if inspect.iscoroutinefunction(getter):
                backend_devices = await getter()
            else:
                backend_devices = getter()

            logger.debug(f"Enumerated {len(backend_devices or [])} backend devices.")
            sanitized_target = (
                sanitize_address(address_to_find) if address_to_find else None
            )
            for device in backend_devices or []:
                metadata = dict(getattr(device, "metadata", None) or {})
                uuids = metadata.get("uuids", [])
                logger.debug(
                    f"  - Device: {device.address} ({device.name}), UUIDs: {uuids}"
                )
                if SERVICE_UUID not in uuids:
                    continue

                if sanitized_target:
                    sanitized_addr = sanitize_address(device.address)
                    sanitized_name = sanitize_address(device.name)
                    if sanitized_target not in (sanitized_addr, sanitized_name):
                        logger.debug(
                            f"    ... skipping, address/name does not match '{sanitized_target}'"
                        )
                        continue

                rssi = getattr(device, "rssi", 0)
                metadata.setdefault("rssi", rssi)
                devices_found.append(
                    build_ble_device(
                        device.address,
                        device.name,
                        metadata,
                        rssi,
                    )
                )
        logger.debug(f"Finished async enumeration, found {len(devices_found)} matching devices.")
        return devices_found

    return _run_coroutine_factory(
        lambda: _get_devices_async(address),
        timeout,
        "connected-device fallback",
    )


class _BLECoroutineRunner:
    """A long-lived thread that runs an asyncio event loop to execute BLE coroutines."""

    def __init__(self):
        self._loop = None
        self._ready = Event()
        self._thread = Thread(target=self._run, name="BLECoroutineRunner", daemon=True)
        self._thread.start()
        atexit.register(self.shutdown)

    def _run(self):
        """Thread entry point."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run_coroutine(self, coro, timeout: float) -> List[BLEDevice]:
        """Submit a coroutine to the event loop and wait for its result."""
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError("BLE coroutine runner thread did not start in time.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def shutdown(self):
        """Stop the event loop and join the thread."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=1)


_runner_instance = _BLECoroutineRunner()


def _run_coroutine_factory(func, timeout: float, label: str) -> List[BLEDevice]:
    """
    Execute `func` (which must return a coroutine) in a dedicated thread with its own asyncio event loop.
    """
    try:
        coro = with_timeout(func(), timeout, label)
        return _runner_instance.run_coroutine(coro, timeout)
    except FutureTimeoutError:
        logger.debug("%s thread exceeded %.1fs timeout", label, timeout)
        return []
    except Exception as exc:  # pragma: no cover  # noqa: BLE001
        logger.debug("%s failed: %s", label, exc)
        return []
