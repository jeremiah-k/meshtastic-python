"""BLE device discovery strategies."""

import asyncio
import inspect
import logging
import time
from abc import ABC, abstractmethod
from typing import List, Optional, Callable, Any, cast

from bleak import BleakScanner, BLEDevice

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    BLEAK_VERSION,
    BLEConfig,
    SERVICE_UUID,
    logger,
)
from meshtastic.interfaces.ble.constants import _bleak_supports_connected_fallback

class DiscoveryStrategy(ABC):
    """Abstract base class for device discovery strategies."""

    @abstractmethod
    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Discover BLE devices already connected to the system via the Bleak backend.
        
        Parameters:
            address (Optional[str]): Optional target address or device name to filter results; comparisons use sanitized forms.
            timeout (float): Maximum time in seconds to wait for backend device enumeration.
        
        Returns:
            List[BLEDevice]: Discovered devices that advertise the module's SERVICE_UUID and, if `address` is provided, match the sanitized address or name. Returns an empty list if the backend does not support connected-device enumeration or on error.
        """

class ConnectedStrategy(DiscoveryStrategy):
    """Device discovery strategy that enumerates already-connected devices."""

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Enumerates already-connected BLE devices via the Bleak backend (when supported) and returns those advertising the configured service UUID, optionally filtered by address or name.
        
        Parameters:
            address (Optional[str]): Target BLE address or device name to filter results; comparison uses the same sanitation applied by the BLE client. If None, no address/name filtering is applied.
            timeout (float): Maximum seconds to wait for the backend's device enumeration to complete.
        
        Returns:
            List[BLEDevice]: Connected devices that advertise SERVICE_UUID and match the optional address/name filter. Returns an empty list if the Bleak backend does not support connected-device enumeration or if an error occurs.
        """
        if not _bleak_supports_connected_fallback():
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
            # Bleak lacks a public API for enumerating already-connected devices; use
            # the private backend hook until upstream provides an official method.
            backend = getattr(scanner, "_backend", None)
            if backend and hasattr(backend, "get_devices"):
                getter = backend.get_devices
                loop = asyncio.get_running_loop()
                # TODO: Replace this private backend access if bleak adds a public API for enumerating connected devices.
                if inspect.iscoroutinefunction(getter):
                    backend_devices = await BLEClient._with_timeout(
                        getter(),
                        timeout,
                        "connected-device enumeration",
                    )
                else:
                    backend_devices = await BLEClient._with_timeout(
                        loop.run_in_executor(None, getter),
                        timeout,
                        "connected-device enumeration",
                    )

                sanitized_target = (
                    BLEClient._sanitize_address(address) if address else None
                )
                for device in backend_devices or []:
                    metadata = getattr(device, "metadata", None) or {}
                    uuids = metadata.get("uuids", [])
                    if SERVICE_UUID not in uuids:
                        continue

                    if sanitized_target:
                        sanitized_addr = BLEClient._sanitize_address(device.address)
                        sanitized_name = BLEClient._sanitize_address(device.name)
                        if sanitized_target not in (sanitized_addr, sanitized_name):
                            continue

                    device_copy = BLEDevice(
                        address=device.address,
                        name=device.name,
                        details=metadata,
                    )
                    # Preserve RSSI if provided by backend
                    if hasattr(device, "rssi"):
                        try:
                            device_copy.rssi = device.rssi  # type: ignore[attr-defined]
                        except AttributeError:  # pragma: no cover - best effort
                            pass
                    devices_found.append(device_copy)
            else:
                logger.debug(
                    "Connected-device enumeration not supported on this bleak backend."
                )
            return devices_found
        except Exception as e:
            logger.debug("Connected device discovery failed: %s", e)
            return []

class DiscoveryManager:
    """Orchestrates scanning + connected-device fallback logic."""

    def __init__(self, client_factory=None):
        """
        Initialize the DiscoveryManager.
        
        Parameters:
            client_factory (optional): Callable or class used to construct BLE client instances; primarily provided for testing or to override the default BLE client.
        """
        # Allow test overrides via meshtastic.ble_interface monkeypatch (backwards compatibility)
        self.client_factory = client_factory
        self.connected_strategy = ConnectedStrategy()

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Discover BLE devices advertising the configured service UUID, and if a target address or name is provided and the scan finds no matches, attempt a fallback enumeration of already-connected devices.
        
        Parameters:
            address (Optional[str]): Bluetooth address or device name to filter results; when provided, triggers a connected-device fallback if the scan yields no matching devices.
        
        Returns:
            List[BLEDevice]: Devices found by the scan and any fallback enumeration, possibly an empty list.
        """
        from meshtastic.interfaces.ble.utils import resolve_ble_module

        ble_mod = resolve_ble_module()
        client_factory: Callable[..., Any] = cast(
            Callable[..., Any],
            self.client_factory or getattr(ble_mod, "BLEClient", BLEClient),
        )
        with client_factory(log_if_no_address=False) as client:
            devices: List[BLEDevice] = []
            sanitized_target = BLEClient._sanitize_address(address) if address else None
            try:
                scan_start = time.monotonic()
                logger.debug(
                    "Scanning for BLE devices (takes %.0f seconds)...",
                    BLEConfig.BLE_SCAN_TIMEOUT,
                )
                response = client.discover(
                    timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                    return_adv=True,
                    service_uuids=[SERVICE_UUID],
                )
                logger.debug("Scan completed in %.2f seconds", time.monotonic() - scan_start)

                if response is None:
                    logger.warning("BleakScanner.discover returned None")
                    response = {}
                elif not isinstance(response, dict):
                    logger.warning(
                        "BleakScanner.discover returned unexpected type: %s",
                        type(response),
                    )
                    response = {}
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
            except Exception as e:
                logger.debug("Device discovery failed: %s", e)

            # If caller requested a specific address/name, filter here so we can
            # fall back to connected-device enumeration when no match is found.
            if sanitized_target:
                devices = [
                    d
                    for d in devices
                    if sanitized_target
                    in (
                        BLEClient._sanitize_address(d.address),
                        BLEClient._sanitize_address(d.name),
                    )
                ]

            if not devices and address:
                logger.debug(
                    "Scan found no devices, trying fallback to already-connected devices"
                )
                connected_coro = self.connected_strategy.discover(
                    address, BLEConfig.BLE_SCAN_TIMEOUT
                )
                try:
                    fallback = client.async_await(
                        connected_coro, timeout=BLEConfig.BLE_SCAN_TIMEOUT
                    )
                    devices.extend(fallback)
                except Exception as e:  # pragma: no cover - best effort logging
                    logger.debug("Connected device fallback failed: %s", e)
                    if inspect.iscoroutine(connected_coro):
                        connected_coro.close()

            return devices
