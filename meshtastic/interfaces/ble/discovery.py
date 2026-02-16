"""BLE device discovery strategies."""

import asyncio
import inspect
import time
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    BLEAK_VERSION,
    SERVICE_UUID,
    BLEConfig,
    _bleak_supports_connected_fallback,
    logger,
)
from meshtastic.interfaces.ble.utils import resolve_ble_module, sanitize_address


@lru_cache(maxsize=1)
def _ble_device_constructor_kwargs_support() -> Tuple[bool, bool]:
    """
    Determine whether BLEDevice.__init__ accepts the keyword arguments "details" and "rssi".

    Returns:
        Tuple[bool, bool]: (supports_details, supports_rssi) where the first element is True if the constructor accepts a `details` kwarg and the second is True if it accepts an `rssi` kwarg.

    """
    sig = inspect.signature(BLEDevice.__init__)
    return ("details" in sig.parameters, "rssi" in sig.parameters)


def parse_scan_response(
    response: Any, whitelist_address: Optional[str] = None
) -> List[BLEDevice]:
    """
    Convert BleakScanner.discover(return_adv=True) output into BLEDevice objects, including devices that advertise SERVICE_UUID or that exactly match an optional whitelist address or name.

    Parameters
    ----------
        response (Any): The value returned by BleakScanner.discover(return_adv=True); expected to be a dict mapping identifiers to (device, adv) tuples.
        whitelist_address (Optional[str]): A sanitized address or device name to include regardless of advertised services; must match device address or name exactly.

    Returns
    -------
        List[BLEDevice]: Devices that advertise SERVICE_UUID or match the provided whitelist_address.

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
        if isinstance(value, tuple) and len(value) == 2:
            device, adv = value
        else:
            logger.warning(
                "Unexpected return type from BleakScanner.discover: %s (len=%s)",
                type(value),
                len(value) if isinstance(value, tuple) else "N/A",
            )
            continue

        # Check for Service UUID
        suuids = getattr(adv, "service_uuids", None)
        has_service = SERVICE_UUID in (suuids or [])

        # Check for whitelist match if provided
        matches_whitelist = False
        if whitelist_address:
            sanitized_addr = sanitize_address(device.address)
            sanitized_name = sanitize_address(device.name)
            # CRITICAL FIX: Use exact equality matching instead of substring matching to prevent
            # connecting to wrong device when device name/address contains target address as substring.
            # This is essential for scenarios where multiple BLE devices are present and user has
            # pre-bonded the target device via bluetoothctl/blueman before starting the relay.
            # Example vulnerability: if whitelist_address is "AA:BB:CC:DD:EE:FF" and device.name
            # contains that string as a substring, it would incorrectly match and connect to wrong device.
            if whitelist_address in (sanitized_addr, sanitized_name):
                matches_whitelist = True

        if has_service or matches_whitelist:
            devices.append(device)
    return devices


class DiscoveryStrategy(ABC):
    """Abstract base class for device discovery strategies."""

    @abstractmethod
    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Enumerates connected BLE devices that advertise the configured service UUID, optionally filtered by a sanitized address or name.

        Parameters
        ----------
            address (Optional[str]): Optional target device address or name; comparisons are performed on sanitized forms.
            timeout (float): Maximum time in seconds to wait for backend device enumeration.

        Returns
        -------
            List[BLEDevice]: Devices that advertise SERVICE_UUID and, if `address` is provided, whose sanitized address or name equals the sanitized target. Returns an empty list on error.

        Raises
        ------
            BleakDBusError: Re-raised when the underlying backend reports a DBus-specific error.

        """


class ConnectedStrategy(DiscoveryStrategy):
    """Device discovery strategy that enumerates already-connected devices.

    .. warning::
       This strategy uses Bleak's private ``_backend`` API to enumerate
       already-connected devices. This is not part of Bleak's public API
       and may break in future Bleak versions. The strategy is version-gated
       via ``_bleak_supports_connected_fallback()`` and will return an
       empty list if the Bleak version is too old or if the private API
       is unavailable.

       See: https://github.com/hbldh/bleak/issues/ for upstream status
       on a public API for connected-device enumeration.
    """

    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Enumerate already-connected BLE devices via the Bleak backend and return those advertising the configured service UUID, optionally filtered by address or name.

        Parameters
        ----------
            address (Optional[str]): Target BLE address or device name to filter results; comparison uses the same sanitation applied by the BLE client. If None, no address/name filtering is applied.
            timeout (float): Maximum seconds to wait for the backend's device enumeration to complete.

        Returns
        -------
            List[BLEDevice]: Connected devices that advertise SERVICE_UUID and, if `address` is provided, whose sanitized address or sanitized name exactly matches the sanitized target. Returns an empty list if the backend does not support connected-device enumeration or if an error occurs.

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
                # TODO: Replace this private backend access if bleak adds a public API for enumerating connected devices.
                if inspect.iscoroutinefunction(getter):
                    backend_devices = await BLEClient._with_timeout(
                        getter(),
                        timeout,
                        "connected-device enumeration",
                    )
                else:
                    loop = asyncio.get_running_loop()
                    backend_devices = await BLEClient._with_timeout(
                        loop.run_in_executor(None, getter),
                        timeout,
                        "connected-device enumeration",
                    )

                sanitized_target = sanitize_address(address) if address else None
                # BLEDevice constructor signature varies across bleak versions.
                # Handle both `details` and legacy `rssi` kwargs when present.
                supports_details, supports_rssi = (
                    _ble_device_constructor_kwargs_support()
                )
                for device in backend_devices or []:
                    metadata = getattr(device, "metadata", None) or {}
                    uuids = metadata.get("uuids", [])
                    if SERVICE_UUID not in uuids:
                        continue

                    if sanitized_target:
                        sanitized_addr = sanitize_address(device.address)
                        sanitized_name = sanitize_address(device.name)
                        if sanitized_target not in (sanitized_addr, sanitized_name):
                            continue

                    params: Dict[str, Any] = {
                        "address": device.address,
                        "name": device.name,
                    }
                    if supports_details:
                        params["details"] = device
                    if supports_rssi:
                        rssi = getattr(device, "rssi", None)
                        if rssi is not None:
                            params["rssi"] = rssi
                    device_copy = BLEDevice(**params)
                    # Preserve RSSI where constructor kwargs are unsupported.
                    # Note: some BLEDevice variants use __slots__ without `rssi`.
                    if not supports_rssi and hasattr(device, "rssi"):
                        try:
                            device_copy.rssi = device.rssi  # type: ignore[attr-defined]
                        except AttributeError:
                            pass
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
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            raise
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

    def __init__(
        self, client_factory: Optional[Callable[..., BLEClient]] = None
    ) -> None:
        """
        Create a DiscoveryManager that orchestrates BLE scanning and connected-device fallback.

        Parameters
        ----------
            client_factory (Optional[Callable[..., BLEClient]]): Optional factory (callable or class) used to construct BLE client instances; primarily provided for testing or to override the default BLE client.

        """
        # Allow test overrides via meshtastic.ble_interface monkeypatch (backwards compatibility)
        self.client_factory = client_factory
        self.connected_strategy = ConnectedStrategy()
        self._client: Optional[BLEClient] = None

    def discover_devices(self, address: Optional[str]) -> List[BLEDevice]:
        """
        Discover BLE devices advertising the configured service UUID and, if a target address or name is provided and the scan yields no matches, attempt a fallback enumeration of already-connected devices.

        Parameters
        ----------
            address (Optional[str]): Bluetooth address or device name to filter results; when provided the scan is run broadly to ensure the target is found and a connected-device fallback will be attempted if the scan finds no matches.

        Returns
        -------
            List[BLEDevice]: Devices found by the scan and any fallback enumeration, possibly an empty list.

        Raises
        ------
            BleakDBusError: If a DBus/BlueZ error occurs during scanning; this error is propagated to the caller.

        """
        if self._client and getattr(self._client, "_closed", False):
            self._client = None
        if self._client is None:
            ble_mod = resolve_ble_module()
            if ble_mod is None:
                logger.debug("No BLE module found; using default BLEClient")
            client_factory: Callable[..., Any] = cast(
                Callable[..., Any],
                self.client_factory or getattr(ble_mod, "BLEClient", BLEClient),
            )
            self._client = client_factory(log_if_no_address=False)
            # Validate factory returned a valid client
            if self._client is None:
                raise RuntimeError(
                    f"Discovery client factory returned None. Factory: {client_factory!r}"
                )

        client = self._client
        devices: List[BLEDevice] = []
        sanitized_target = sanitize_address(address) if address else None
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
            coro_consumed = False
            try:
                fallback = client.async_await(
                    connected_coro, timeout=BLEConfig.BLE_SCAN_TIMEOUT
                )
                coro_consumed = True
                devices.extend(fallback)
            except (SystemExit, KeyboardInterrupt):
                raise
            except Exception as e:  # pragma: no cover - best effort logging
                logger.warning("Connected device fallback failed: %s", e, exc_info=True)
            finally:
                # Close the coroutine if it was never consumed (async_await raised before scheduling)
                if not coro_consumed and inspect.iscoroutine(connected_coro):
                    try:
                        connected_coro.close()
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "Error closing unconsumed connected-device fallback coroutine",
                            exc_info=True,
                        )

        return devices

    def close(self) -> None:
        """
        Close the manager's persistent discovery client and clear the internal reference.

        If a client exists, attempts to close it and then sets the internal _client to None.
        """
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None

    def __del__(self) -> None:
        """
        Close and clear the internal BLE client when the DiscoveryManager is garbage-collected.

        Performs a best-effort close of the client, suppressing any exceptions and clearing the internal reference to avoid resource leaks during interpreter shutdown.
        """
        # Use getattr to handle cases where __init__ didn't complete
        client = getattr(self, "_client", None)
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                # Best-effort cleanup during GC/interpreter shutdown; avoid logging
                # because logging infrastructure may already be partially torn down.
                pass
            finally:
                self._client = None
