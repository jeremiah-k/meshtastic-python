"""BLE discovery strategies"""
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List, Optional

from bleak import BleakScanner, BLEDevice

from bleak import BleakScanner, BLEDevice
from .config import BLEConfig
from .gatt import SERVICE_UUID
from .util import (
    BLEAK_VERSION,
    _bleak_supports_connected_fallback,
    _sanitize_address,
    _with_timeout,
)

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
        except Exception as e:
            logger.debug("Connected device discovery failed: %s", e)
            return []


class DiscoveryManager:
    """Orchestrates scanning + connected-device fallback logic."""

    def __init__(self):
        self.connected_strategy = ConnectedStrategy()

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        try:
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...",
                BLEConfig.BLE_SCAN_TIMEOUT,
            )

            async def run_scan():
                return await BleakScanner.discover(
                    timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                    service_uuids=[SERVICE_UUID],
                )

            response = asyncio.run(run_scan())

            devices: List[BLEDevice] = []
            if response is None:
                logger.warning("BleakScanner.discover returned None")
            else:
                for device in response:
                    if SERVICE_UUID in device.metadata.get("uuids", []):
                        devices.append(device)

            if not devices and address:
                logger.debug(
                    "Scan found no devices, trying fallback to already-connected devices"
                )
                try:
                    fallback = asyncio.run(
                        self.connected_strategy.discover(
                            address, BLEConfig.BLE_SCAN_TIMEOUT
                        )
                    )
                    devices.extend(fallback)
                except Exception as e:  # pragma: no cover - best effort logging
                    logger.debug("Connected device fallback failed: %s", e)

            return devices
        except Exception as e:
            logger.debug("Device discovery failed: %s", e)
            return []
