"""BLE device discovery strategies."""

import asyncio
import importlib
import inspect
import logging
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
        """Return a list of BLEDevice entries discovered via this strategy."""

class ConnectedStrategy(DiscoveryStrategy):
    """Device discovery strategy that enumerates already-connected devices."""

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
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
            backend = getattr(scanner, "_backend", None)
            if backend and hasattr(backend, "get_devices"):
                import inspect

                getter = backend.get_devices
                loop = asyncio.get_running_loop()
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

                    rssi = getattr(device, "rssi", 0)
                    devices_found.append(
                        BLEDevice(
                            address=device.address,
                            name=device.name,
                            details=metadata,
                            rssi=rssi,
                        )
                    )
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
        # Allow test overrides via meshtastic.ble_interface monkeypatch (backwards compatibility)
        self.client_factory = client_factory
        self.connected_strategy = ConnectedStrategy()

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        ble_mod = None
        for module_name in ("meshtastic.interfaces.ble", "meshtastic.ble_interface"):
            try:
                ble_mod = importlib.import_module(module_name)  # type: ignore[assignment]
                break
            except ImportError:  # pragma: no cover - defensive fallback
                continue
        client_factory: Callable[..., Any] = cast(
            Callable[..., Any],
            self.client_factory or getattr(ble_mod, "BLEClient", BLEClient),
        )
        with client_factory(log_if_no_address=False) as client:
            devices: List[BLEDevice] = []
            sanitized_target = BLEClient._sanitize_address(address) if address else None
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
                    async_await_fn = getattr(client, "async_await")
                    async_await_sig = inspect.signature(async_await_fn)
                    if "timeout" in async_await_sig.parameters:
                        fallback = async_await_fn(
                            connected_coro, timeout=BLEConfig.BLE_SCAN_TIMEOUT
                        )
                    else:
                        fallback = async_await_fn(connected_coro)
                    devices.extend(fallback)
                except Exception as e:  # pragma: no cover - best effort logging
                    logger.debug("Connected device fallback failed: %s", e)
                    if inspect.iscoroutine(connected_coro):
                        connected_coro.close()

            return devices
