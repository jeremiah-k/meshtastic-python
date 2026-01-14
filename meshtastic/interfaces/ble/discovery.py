"""BLE device discovery strategies."""

import asyncio
import inspect
import logging
import time
from abc import ABC, abstractmethod
from typing import List, Optional, Callable, Any, cast

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    BLEAK_VERSION,
    BLEConfig,
    SERVICE_UUID,
    logger,
)
from meshtastic.interfaces.ble.constants import _bleak_supports_connected_fallback
from meshtastic.interfaces.ble.utils import resolve_ble_module


def parse_scan_response(
    response: Any, whitelist_address: Optional[str] = None
) -> List[BLEDevice]:
    """
    Convert a BleakScanner.discover(return_adv=True) response into a list of BLEDevice instances.

    Filters devices that advertise SERVICE_UUID, unless they match the optional `whitelist_address`.

    Parameters:
        response: The dictionary returned by BleakScanner.discover.
        whitelist_address: Optional sanitized address/name to include regardless of UUIDs.
    """
    devices: List[BLEDevice] = []
    if response is None:
        logger.warning("BleakScanner.discover returned None")
        return devices
    if not isinstance(response, dict):
        logger.warning(
            "BleakScanner.discover returned unexpected type: %s",
            type(response),
        )
        return devices
    for _, value in response.items():
        if isinstance(value, tuple):
            device, adv = value
        else:
            logger.warning(
                "Unexpected return type from BleakScanner.discover: %s",
                type(value),
            )
            continue

        # Check for Service UUID
        suuids = getattr(adv, "service_uuids", None)
        has_service = SERVICE_UUID in (suuids or [])

        # Check for whitelist match if provided
        matches_whitelist = False
        if whitelist_address:
            sanitized_addr = BLEClient._sanitize_address(device.address)
            sanitized_name = BLEClient._sanitize_address(device.name)
            # CRITICAL FIX: Use exact equality matching instead of substring matching to prevent
            # connecting to wrong device when device name/address contains target address as substring.
            # This is essential for scenarios where multiple BLE devices are present and user has
            # pre-bonded the target device via bluetoothctl/blueman before starting the relay.
            # Example vulnerability: if whitelist_address is "AA:BB:CC:DD:EE:FF" and device.name
            # contains that string as a substring, it would incorrectly match and connect to wrong device.
            if (
                whitelist_address == sanitized_addr
                or whitelist_address == sanitized_name
            ):
                matches_whitelist = True

        if has_service or matches_whitelist:
            devices.append(device)
    return devices


class DiscoveryStrategy(ABC):
    """Abstract base class for device discovery strategies."""

    @abstractmethod
    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Discover BLE devices using the strategy's underlying mechanism (scan, connected enumeration, cache, etc.).

        Parameters:
            address (Optional[str]): Optional target address or device name to filter results; comparisons use sanitized forms.
            timeout (float): Maximum time in seconds to wait for backend device enumeration.

        Returns:
            List[BLEDevice]: Discovered devices that advertise the module's SERVICE_UUID and, if `address` is provided, match the sanitized address or name. Returns an empty list on error.
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
                        device_copy.rssi = device.rssi  # type: ignore[attr-defined]
                    devices_found.append(device_copy)
            else:
                logger.debug(
                    "Connected-device enumeration not supported on this bleak backend."
                )
                return []
        except BleakDBusError as e:
            logger.warning(
                "Connected device discovery failed due to DBus error: %s",
                e,
                exc_info=True,
            )
            raise
        except (BleakError, RuntimeError) as e:
            logger.warning("Connected device discovery failed: %s", e, exc_info=True)
            return []
        except Exception as e:  # pragma: no cover - defensive last resort
            # Defensive last resort to keep discovery best-effort
            logger.warning(
                "Unexpected error during connected device discovery: %s",
                e,
                exc_info=True,
            )
            return []
        else:
            return devices_found


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
        self._client: Optional[BLEClient] = None

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Discover BLE devices advertising the configured service UUID, and if a target address or name is provided and the scan finds no matches, attempt a fallback enumeration of already-connected devices.

        Parameters:
            address (Optional[str]): Bluetooth address or device name to filter results; when provided, triggers a connected-device fallback if the scan yields no matching devices.

        Returns:
            List[BLEDevice]: Devices found by the scan and any fallback enumeration, possibly an empty list.
        """
        if self._client:
            event_thread = getattr(self._client, "_eventThread", None)
            if getattr(self._client, "_closed", False) or (
                event_thread and not event_thread.is_alive()
            ):
                try:
                    self._client.close()
                except Exception:  # pragma: no cover - best effort cleanup
                    logger.warning(
                        "Error closing stale discovery client", exc_info=True
                    )
                finally:
                    self._client = None
        if self._client is None:
            ble_mod = resolve_ble_module()
            client_factory: Callable[..., Any] = cast(
                Callable[..., Any],
                self.client_factory or getattr(ble_mod, "BLEClient", BLEClient),
            )
            self._client = client_factory(log_if_no_address=False)

        client = self._client
        devices: List[BLEDevice] = []
        sanitized_target = BLEClient._sanitize_address(address) if address else None
        try:
            scan_start = time.monotonic()
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...",
                BLEConfig.BLE_SCAN_TIMEOUT,
            )

            # If we are looking for a specific address, scan everything to ensure we find it
            # even if the Service UUID is missing from the advertisement.
            scan_uuids = [SERVICE_UUID] if not sanitized_target else None

            response = client.discover(
                timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                return_adv=True,
                service_uuids=scan_uuids,
            )
            logger.debug(
                "Scan completed in %.2f seconds", time.monotonic() - scan_start
            )

            devices = parse_scan_response(response, whitelist_address=sanitized_target)
        except BleakDBusError as e:
            # Bubble up BlueZ/DBus failures so callers can back off more aggressively
            logger.warning(
                "Device discovery failed due to DBus error: %s", e, exc_info=True
            )
            raise
        except (BleakError, RuntimeError) as e:
            logger.warning("Device discovery failed: %s", e, exc_info=True)
            devices = []
        except Exception as e:  # pragma: no cover - defensive last resort
            # Defensive last resort to keep discovery best-effort
            logger.warning(
                "Unexpected error during device discovery: %s", e, exc_info=True
            )
            devices = []

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
                logger.warning("Connected device fallback failed: %s", e, exc_info=True)
                if inspect.iscoroutine(connected_coro):
                    connected_coro.close()

        return devices

    def close(self) -> None:
        """Release persistent discovery client resources."""
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None
