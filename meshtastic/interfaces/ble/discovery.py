"""BLE discovery strategies."""
import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, TYPE_CHECKING

from bleak import BLEDevice, BleakScanner

from .client import BLEClient
from .config import BLEAK_VERSION, BLEConfig, SERVICE_UUID
from .util import (
    bleak_supports_connected_fallback,
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
        """Return a list of BLEDevice entries discovered via this strategy."""


class ConnectedStrategy(DiscoveryStrategy):
    """Device discovery strategy that enumerates already-connected devices."""

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
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
                    backend_devices = await with_timeout(
                        getter(),
                        timeout,
                        "connected-device enumeration",
                    )
                else:
                    backend_devices = await with_timeout(
                        loop.run_in_executor(None, getter),
                        timeout,
                        "connected-device enumeration",
                    )

                sanitized_target = (
                    sanitize_address(address) if address else None
                )
                for device in backend_devices or []:
                    metadata = dict(getattr(device, "metadata", None) or {})
                    uuids = metadata.get("uuids", [])
                    if SERVICE_UUID not in uuids:
                        continue

                    if sanitized_target:
                        sanitized_addr = sanitize_address(device.address)
                        sanitized_name = sanitize_address(device.name)
                        if sanitized_target not in (sanitized_addr, sanitized_name):
                            continue

                    rssi = getattr(device, "rssi", 0)
                    metadata.setdefault("rssi", rssi)
                    devices_found.append(
                        BLEDevice(
                            device.address,
                            device.name,
                            metadata,
                        )
                    )
            return devices_found
        except Exception as e:  # pragma: no cover  # noqa: BLE001 - best-effort fallback
            logger.debug("Connected device discovery failed: %s", e)
            return []


class DiscoveryManager:
    """Orchestrates scanning + connected-device fallback logic."""

    def __init__(self):
        self.connected_strategy = ConnectedStrategy()

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
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

                if not devices and address:
                    logger.debug(
                        "Scan found no devices, trying fallback to already-connected devices"
                    )
                    try:
                        fallback = client.async_await(
                            self.connected_strategy.discover(
                                address, BLEConfig.BLE_SCAN_TIMEOUT
                            )
                        )
                        devices.extend(fallback)
                    except Exception as e:  # pragma: no cover  # noqa: BLE001
                        logger.debug("Connected device fallback failed: %s", e)

                return devices
            except Exception as e:  # noqa: BLE001 - discovery must never raise
                logger.debug("Device discovery failed: %s", e)
                return []
