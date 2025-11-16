"""Discovery logic for locating Meshtastic BLE devices."""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Thread
from typing import List, Optional, TYPE_CHECKING

from bleak import BLEDevice, BleakScanner

import sys

from .client import BLEClient
from .config import BLEConfig, BLEAK_VERSION, SERVICE_UUID, logger, bleak_supports_connected_fallback
from .utils import sanitize_address, with_timeout

if TYPE_CHECKING:  # pragma: no cover - imported only for typing
    from bleak.exc import BleakDBusError, BleakError


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
                # NOTE: Bleak does not yet expose connected-device enumeration via a
                # public API, so we rely on the backend's private get_devices()
                # helper. Keep this guarded and documented so reviewers know the
                # trade-off is intentional.
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

                sanitized_target = sanitize_address(address) if address else None
                for device in backend_devices or []:
                    metadata = getattr(device, "metadata", None) or {}
                    uuids = metadata.get("uuids", [])
                    if SERVICE_UUID not in uuids:
                        continue

                    if sanitized_target:
                        sanitized_addr = sanitize_address(device.address)
                        sanitized_name = (
                            sanitize_address(device.name) if device.name else None
                        )
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
        except Exception as e:  # pragma: no cover - best-effort guard
            logger.debug("Connected device discovery failed: %s", e)
            return []


class DiscoveryManager:
    """Run scans and fall back to already-connected device enumeration."""

    def __init__(self):
        self.connected_strategy = ConnectedStrategy()

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        client_cls = _resolve_ble_client_class()
        with client_cls(log_if_no_address=False) as client:
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
                    except Exception as e:  # pragma: no cover - best effort logging
                        logger.debug("Connected device fallback failed: %s", e)

                return devices
            except Exception as e:  # pragma: no cover - best-effort guard
                logger.debug("Device discovery failed: %s", e)
                return []


def run_connected_device_lookup(
    strategy: ConnectedStrategy, address: Optional[str]
) -> List[BLEDevice]:
    """Synchronous helper for connected-device discovery."""

    async def _run():
        return await strategy.discover(address, BLEConfig.BLE_SCAN_TIMEOUT)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())

    future: Future[List[BLEDevice]] = Future()

    def _runner():
        try:
            result = asyncio.run(_run())
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)

    thread = Thread(target=_runner, daemon=True)
    thread.start()
    try:
        return future.result(timeout=BLEConfig.BLE_SCAN_TIMEOUT)
    except FutureTimeoutError:
        logger.debug(
            "Fallback connected-device discovery thread exceeded %.1fs timeout",
            BLEConfig.BLE_SCAN_TIMEOUT,
        )
        thread.join(timeout=0.1)
        return []
    except Exception as exc:  # pragma: no cover - best-effort guard
        logger.debug("Connected device discovery thread failed: %s", exc)
        thread.join(timeout=0.1)
        return []


def _resolve_ble_client_class():
    """Return the BLEClient class, honoring dynamic replacements via the public shim."""
    ble_module = sys.modules.get("meshtastic.ble_interface")
    if ble_module:
        candidate = getattr(ble_module, "BLEClient", None)
        if candidate is not None:
            return candidate
    return BLEClient


__all__ = ["ConnectedStrategy", "DiscoveryManager", "run_connected_device_lookup"]
