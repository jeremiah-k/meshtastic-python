"""BLE discovery strategies"""

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, List, Optional, TYPE_CHECKING

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from .config import BLEConfig
from .gatt import SERVICE_UUID
from .exceptions import BLEError
from .util import (
    BLEAK_VERSION,
    _bleak_supports_connected_fallback,
    _sanitize_address,
    _with_timeout,
)

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from .client import BLEClient

AsyncExecutor = Callable[[Awaitable[Any]], Any]

logger = logging.getLogger(__name__)


class DiscoveryStrategy(ABC):
    """Abstract base class for device discovery strategies."""

    @abstractmethod
    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Discover BLE devices that are already connected and advertise the configured service UUID.
        
        If `address` is provided, only devices whose sanitized address or name matches the sanitized `address` are returned. The `timeout` is used when querying backend device getters that may be asynchronous.
        
        Parameters:
            address (Optional[str]): Optional address or name filter to narrow results.
            timeout (float): Maximum time, in seconds, to wait for backend device enumeration.
        
        Returns:
            List[BLEDevice]: BLEDevice entries that advertise the configured service UUID and match the optional address filter.
        """


class ConnectedStrategy(DiscoveryStrategy):
    """Device discovery strategy that enumerates already-connected devices."""

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Discover BLE devices that are already connected and expose the service UUID used by this application.
        
        Parameters:
            address (Optional[str]): Optional device address or name to filter results; comparison is performed against a sanitized form of the device address and name.
            timeout (float): Maximum seconds to wait for backend connected-device enumeration.
        
        Returns:
            List[BLEDevice]: Devices that advertise the configured SERVICE_UUID and match the optional address filter. Returns an empty list if scanning fails due to BLE/Bleak timeouts or OS errors; unexpected exceptions are re-raised.
        """
        if not _bleak_supports_connected_fallback(
            BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
        ):
            logger.debug(
                "Skipping fallback connected-device scan; bleak %s < %s",
                BLEAK_VERSION,
                ".".join(
                    str(part)
                    for part in BLEConfig.BLEAK_CONNECTED_DEVICE_FALLBACK_MIN_VERSION
                ),
            )
            return []

        try:
            scanner = BleakScanner()
            devices_found: List[BLEDevice] = []
            if hasattr(scanner, "_backend") and hasattr(
                scanner._backend, "get_devices"
            ):
                getter = scanner._backend.get_devices
                loop = asyncio.get_running_loop()
                if inspect.iscoroutinefunction(getter):
                    backend_devices = await _with_timeout(
                        getter(),
                        timeout,
                        "connected-device enumeration",
                    )
                else:
                    backend_devices = await _with_timeout(
                        loop.run_in_executor(None, getter),
                        timeout,
                        "connected-device enumeration",
                    )

                sanitized_target = _sanitize_address(address) if address else None
                for device in backend_devices or []:
                    metadata = getattr(device, "metadata", None) or {}
                    uuids = metadata.get("uuids", [])
                    if SERVICE_UUID not in uuids:
                        continue

                    if sanitized_target:
                        sanitized_addr = _sanitize_address(device.address)
                        sanitized_name = _sanitize_address(device.name)
                        if sanitized_target not in (sanitized_addr, sanitized_name):
                            continue

                    rssi = getattr(device, "rssi", 0)
                    devices_found.append(
                        BLEDevice(
                            device.address,
                            device.name,
                            metadata,
                            rssi,
                        )
                    )
            return devices_found
        except (
            BLEError,
            BleakError,
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
        ) as exc:
            logger.warning("Connected device discovery failed: %s", exc)
            return []
        except Exception:  # pragma: no cover - unexpected failures
            logger.exception("Unexpected error during connected-device discovery")
            raise


class DiscoveryManager:
    """Orchestrates scanning + connected-device fallback logic."""

    def __init__(
        self,
        *,
        ble_client_factory: Optional[Callable[[], "BLEClient"]] = None,
    ):
        """
        Initialize a DiscoveryManager with an optional BLE client factory and a ConnectedStrategy.
        
        Parameters:
            ble_client_factory (Optional[Callable[[], BLEClient]]): Optional factory that returns a BLE client instance; if omitted, an internal factory that creates a default BLE client is used.
        """
        self.connected_strategy = ConnectedStrategy()
        self._ble_client_factory = ble_client_factory or self._create_ble_client

    @staticmethod
    def _create_ble_client() -> "BLEClient":
        """
        Create and return a BLEClient instance configured for internal use.
        
        Returns:
            BLEClient: A newly constructed BLEClient with `log_if_no_address` set to False.
        """
        from .client import BLEClient

        return BLEClient(log_if_no_address=False)

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Orchestrates BLE device discovery using an appropriate execution strategy and handles retries on event-loop conflicts.
        
        Attempts discovery using an async runner when a running event loop is present, otherwise uses the provided executor. On detection of an event-loop conflict it retries discovery with an async runner. Known BLE-related errors result in an empty result; unexpected exceptions are re-raised.
        
        Parameters:
            address (Optional[str]): Optional device address or name to filter discovery results.
        
        Returns:
            List[BLEDevice]: List of discovered BLEDevice objects (may be empty).
        """
        use_runner = self._should_use_async_runner()
        try:
            if use_runner:
                return self._discover_with_async_runner(address)
            return self._discover_with_executor(address, asyncio.run)
        except RuntimeError as runtime_error:
            if not use_runner and self._is_event_loop_conflict(runtime_error):
                logger.debug(
                    "Detected active event loop; retrying discovery on async runner."
                )
                try:
                    return self._discover_with_async_runner(address)
                except (
                    BLEError,
                    BleakError,
                    TimeoutError,
                    asyncio.TimeoutError,
                    OSError,
                ) as exc:
                    logger.warning(
                        "Device discovery failed after retrying on async runner: %s",
                        exc,
                    )
                    return []
            raise
        except (
            BLEError,
            BleakError,
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
        ) as exc:
            logger.warning("Device discovery failed: %s", exc)
            return []
        except Exception:
            logger.exception("Unexpected error during BLE discovery")
            raise

    def _discover_with_async_runner(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Perform device discovery using an asynchronous BLE client runner.
        
        Parameters:
            address (Optional[str]): Optional target device address to filter discovery results.
        
        Returns:
            List[BLEDevice]: Discovered BLEDevice entries matching the configured service UUID and optional address filter.
        """
        with self._ble_client_factory() as runner:
            return self._discover_with_executor(address, runner.async_await)

    def _discover_with_executor(
        self,
        address: Optional[str],
        executor: AsyncExecutor,
    ) -> List[BLEDevice]:
        """
        Perform a BLE scan for devices advertising the configured service UUID and return matching devices, with an optional fallback to already-connected devices when none are found.
        
        Performs a scan via the given executor using BleakScanner.discover filtered by SERVICE_UUID, filters the discovered devices to those that advertise SERVICE_UUID, and if no devices are found and `address` is provided, invokes the connected-device fallback discovery and includes any returned devices.
        
        Parameters:
            address (Optional[str]): If provided and the initial scan yields no results, used to restrict the connected-device fallback to a specific device address.
            executor (AsyncExecutor): Callable that accepts an awaitable and returns its result (used to run the BleakScanner.discover call and the connected-device fallback).
        
        Returns:
            List[BLEDevice]: Devices that advertise SERVICE_UUID, possibly including devices returned by the connected-device fallback.
        
        Raises:
            Exception: Re-raises unexpected exceptions originating from the scan or fallback operations; known scan/fallback errors (BLEError, BleakError, TimeoutError, asyncio.TimeoutError, OSError) are handled and do not propagate.
        """
        logger.debug(
            "Scanning for BLE devices (takes %.0f seconds)...",
            BLEConfig.BLE_SCAN_TIMEOUT,
        )

        try:
            response = executor(
                BleakScanner.discover(
                    timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                    service_uuids=[SERVICE_UUID],
                )
            )
        except RuntimeError:
            raise
        except (
            BLEError,
            BleakError,
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
        ) as scan_error:
            logger.warning("BLE scan failed: %s", scan_error)
            response = None
        except Exception:
            logger.exception("Unexpected error while scanning for BLE devices")
            raise

        devices: List[BLEDevice] = []
        if response is None:
            logger.warning("BleakScanner.discover returned None")
        else:
            for device in response:
                # Handle different bleak versions
                uuids = []
                if hasattr(device, "metadata"):
                    uuids = device.metadata.get("uuids", [])
                elif hasattr(device, "details") and isinstance(device.details, dict):
                    props = device.details.get("props", {})
                    uuids = props.get("UUIDs", [])

                logger.debug(f"Device {device.name} has UUIDs: {uuids}")
                if SERVICE_UUID in uuids:
                    logger.debug(f"Adding device {device.name} to results")
                    devices.append(device)
                else:
                    logger.debug(f"Skipping device {device.name} - no SERVICE_UUID")

        if not devices and address:
            logger.debug(
                "Scan found no devices, trying fallback to already-connected devices"
            )
            try:
                fallback = executor(
                    self.connected_strategy.discover(
                        address, BLEConfig.BLE_SCAN_TIMEOUT
                    )
                )
                if fallback:
                    devices.extend(fallback)
            except (
                BLEError,
                BleakError,
                TimeoutError,
                asyncio.TimeoutError,
                OSError,
            ) as exc:
                logger.warning("Connected device fallback failed: %s", exc)
            except Exception:
                logger.exception("Unexpected error in connected-device fallback")
                raise

        return devices

    @staticmethod
    def _should_use_async_runner() -> bool:
        """
        Determine whether the current thread has an active asyncio event loop.
        
        Returns:
            `True` if an event loop is currently running, `False` otherwise.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    @staticmethod
    def _is_event_loop_conflict(error: RuntimeError) -> bool:
        """
        Detects whether a RuntimeError indicates an event-loop conflict (attempt to run an asyncio loop while another is running).
        
        Parameters:
            error (RuntimeError): The runtime error to inspect.
        
        Returns:
            bool: `True` if the error message matches known asyncio event-loop conflict signatures, `False` otherwise.
        """
        message = str(error)
        conflict_signatures = (
            "asyncio.run() cannot be called from a running event loop",
            "Cannot run the event loop while another loop is running",
        )
        return any(signature in message for signature in conflict_signatures)