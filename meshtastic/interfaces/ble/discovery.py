"""BLE discovery strategies"""

import asyncio
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
        """Return a list of BLEDevice entries discovered via this strategy."""


class ConnectedStrategy(DiscoveryStrategy):
    """Device discovery strategy that enumerates already-connected devices."""

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
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
                import inspect

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
        except (BLEError, BleakError, TimeoutError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Connected device discovery failed: %s", exc)
            return []
        except Exception as exc:  # pragma: no cover - unexpected failures
            logger.exception("Unexpected error during connected-device discovery")
            raise


class DiscoveryManager:
    """Orchestrates scanning + connected-device fallback logic."""

    def __init__(
        self,
        *,
        ble_client_factory: Optional[Callable[[], "BLEClient"]] = None,
    ):
        self.connected_strategy = ConnectedStrategy()
        self._ble_client_factory = ble_client_factory or self._create_ble_client

    @staticmethod
    def _create_ble_client() -> "BLEClient":
        from .client import BLEClient

        return BLEClient(log_if_no_address=False)

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
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
        except (BLEError, BleakError, TimeoutError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Device discovery failed: %s", exc)
            return []
        except Exception:
            logger.exception("Unexpected error during BLE discovery")
            raise

    def _discover_with_async_runner(self, address: Optional[str]) -> List[BLEDevice]:
        with self._ble_client_factory() as runner:
            return self._discover_with_executor(address, runner.async_await)

    def _discover_with_executor(
        self,
        address: Optional[str],
        executor: AsyncExecutor,
    ) -> List[BLEDevice]:
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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    @staticmethod
    def _is_event_loop_conflict(error: RuntimeError) -> bool:
        message = str(error)
        conflict_signatures = (
            "asyncio.run() cannot be called from a running event loop",
            "Cannot run the event loop while another loop is running",
        )
        return any(signature in message for signature in conflict_signatures)
