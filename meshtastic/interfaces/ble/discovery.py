"""BLE device discovery strategies."""

import asyncio
import contextlib
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


def _normalize_device_name_for_matching(name: Optional[str]) -> Optional[str]:
    """
    Normalize a Bluetooth device name for tolerant comparisons.
    
    Strips leading/trailing whitespace and applies casefolding; preserves punctuation. If the input is None or reduces to an empty string after normalization, returns `None`.
    
    Parameters:
        name (Optional[str]): Raw device name.
    
    Returns:
        Optional[str]: The normalized name (`name.strip().casefold()`), or `None` if input is None or empty after normalization.
    """
    if name is None:
        return None
    normalized_name = name.strip().casefold()
    return normalized_name or None


def _filter_devices_for_target_identifier(
    devices: List[BLEDevice], target_identifier: str
) -> List[BLEDevice]:
    """
    Selects BLEDevice objects matching a user-supplied address or name using deterministic precedence.
    
    Matching precedence:
    1) Exact normalized address match.
    2) Exact name match (case-sensitive).
    3) Normalized name match (casefolded and stripped) only when exactly one candidate matches.
    
    Parameters:
        devices (List[BLEDevice]): Candidate devices to search.
        target_identifier (str): User-supplied address or device name to match.
    
    Returns:
        List[BLEDevice]: Devices that match according to the precedence rules. Returns an empty list when no match is found or when multiple devices match by normalized name (ambiguous).
    """
    target_key = sanitize_address(target_identifier)
    if target_key:
        address_matches = [
            device
            for device in devices
            if sanitize_address(getattr(device, "address", None)) == target_key
        ]
        if address_matches:
            return address_matches

    exact_name_matches = [
        device
        for device in devices
        if getattr(device, "name", None) == target_identifier
    ]
    if exact_name_matches:
        return exact_name_matches

    normalized_target_name = _normalize_device_name_for_matching(target_identifier)
    if normalized_target_name is None:
        return []

    normalized_name_matches = [
        device
        for device in devices
        if _normalize_device_name_for_matching(getattr(device, "name", None))
        == normalized_target_name
    ]
    if len(normalized_name_matches) == 1:
        return normalized_name_matches
    if len(normalized_name_matches) > 1:
        logger.warning(
            "Ambiguous device-name match for %r after normalized comparison (%d candidates); use exact BLE address or exact name.",
            target_identifier,
            len(normalized_name_matches),
        )
    return []


def _parse_scan_response(
    response: Any, whitelist_address: Optional[str] = None
) -> List[BLEDevice]:
    """
    Convert BleakScanner.discover(return_adv=True) output into BLEDevice objects.

    When `whitelist_address` is provided, matches are selected using
    address-first and name-aware precedence:
    1) exact normalized address key, 2) exact name, 3) normalized name
    (`casefold().strip()`) only if unique. Otherwise, devices advertising
    `SERVICE_UUID` are returned.

    Parameters
    ----------
        response (Any): The value returned by BleakScanner.discover(return_adv=True); expected to be a dict mapping identifiers to (device, adv) tuples.
        whitelist_address (Optional[str]): Raw address or device name target.

    Returns
    -------
        List[BLEDevice]: Devices matching the target (targeted mode) or devices
            advertising SERVICE_UUID (broad scan mode).

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
    target_identifier = whitelist_address.strip() if whitelist_address else None
    has_whitelist = bool(target_identifier)
    target_candidates: List[BLEDevice] = []
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

        if has_whitelist:
            target_candidates.append(device)
        elif has_service:
            devices.append(device)

    if has_whitelist and target_identifier is not None:
        return _filter_devices_for_target_identifier(
            target_candidates, target_identifier
        )
    return devices


class DiscoveryStrategy(ABC):
    """Abstract base class for device discovery strategies."""

    @abstractmethod
    async def discover(self, address: Optional[str], timeout: float) -> List[BLEDevice]:
        """
        Enumerates connected BLE devices that advertise the configured service UUID, optionally filtered by address or name.

        Parameters
        ----------
            address (Optional[str]): Optional target device address or name.
            timeout (float): Maximum time in seconds to wait for backend device enumeration.

        Returns
        -------
            List[BLEDevice]: Devices that advertise SERVICE_UUID and, if
            `address` is provided, match via address-first and name-aware
            precedence rules. Returns an empty list on error.

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
            address (Optional[str]): Target BLE address or device name to
                filter results. If None, no address/name filtering is applied.
            timeout (float): Maximum seconds to wait for the backend's device enumeration to complete.

        Returns
        -------
            List[BLEDevice]: Connected devices that advertise SERVICE_UUID and,
            if `address` is provided, match via address-first and name-aware
            precedence rules. Returns an empty list if the backend does not
            support connected-device enumeration or if an error occurs.

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

                target_candidates: List[BLEDevice] = []
                target_identifier = address.strip() if address else None
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

                    target_candidates.append(device)

                if target_identifier:
                    matched_candidates = _filter_devices_for_target_identifier(
                        target_candidates, target_identifier
                    )
                else:
                    matched_candidates = target_candidates

                for device in matched_candidates:
                    # Pass address and name as positional args to avoid DeprecationWarning
                    # in bleak 2.1.x where kwargs for these are deprecated.
                    params: Dict[str, Any] = {}
                    if supports_details:
                        # Pass the original device object as details so the backend
                        # can recognize it as an already-connected device.
                        params["details"] = device
                    if supports_rssi:
                        rssi = getattr(device, "rssi", None)
                        if rssi is not None:
                            params["rssi"] = rssi
                    device_copy = BLEDevice(device.address, device.name, **params)
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
        Create a DiscoveryManager that orchestrates BLE scanning and a connected-device fallback.

        Parameters
        ----------
            client_factory (Optional[Callable[..., BLEClient]]): Optional factory used to construct BLE client instances; primarily for testing or to override the default BLE client.

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
        # Only discard the client if it was previously connected and has since
        # disconnected. A discovery-only client (never connected) should be reused.
        if self._client is not None:
            bleak = getattr(self._client, "bleak_client", None)
            if bleak is not None and not self._client.is_connected():
                self._client = None
        if self._client is None:
            # Factory resolution precedence (back-compat and testability):
            #   1. Explicit self.client_factory (injected for testing)
            #   2. Monkeypatched ble_mod.BLEClient (legacy/back-compat shim)
            #   3. Directly imported BLEClient (default)
            ble_mod = resolve_ble_module()
            if ble_mod is None:
                logger.debug("No BLE module found; using default BLEClient")
            resolved_factory: Callable[..., Any] = cast(
                Callable[..., Any],
                self.client_factory or getattr(ble_mod, "BLEClient", BLEClient),
            )
            # Attempt to create client with log_if_no_address=False; fall back
            # for custom factories that don't accept this kwarg.
            try:
                self._client = resolved_factory(log_if_no_address=False)
            except TypeError:
                self._client = resolved_factory()
            # Validate factory returned a valid client (duck typing for testability)
            if self._client is None:
                raise RuntimeError(
                    f"Discovery client factory returned None. Factory: {resolved_factory!r}"
                )
            # Accept BLEClient instances or any object with the required interface
            # (duck typing allows test fixtures while still catching errors)
            if not isinstance(self._client, BLEClient):
                required_attrs = (
                    "discover",
                    "async_await",
                    "__enter__",
                    "__exit__",
                )
                missing = [a for a in required_attrs if not hasattr(self._client, a)]
                if missing:
                    raise RuntimeError(
                        f"Discovery client factory returned invalid type. "
                        f"Factory: {resolved_factory!r}, returned type: {type(self._client)!r}, "
                        f"missing required attributes: {missing}"
                    )

        client = self._client
        devices: List[BLEDevice] = []
        target_identifier = address.strip() if address else None
        try:
            scan_start = time.monotonic()
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...",
                BLEConfig.BLE_SCAN_TIMEOUT,
            )

            # If we are looking for a specific address, scan everything to ensure we find it
            # even if the Service UUID is missing from the advertisement.
            scan_uuids = [SERVICE_UUID] if not target_identifier else None

            response = client.discover(
                timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                return_adv=True,
                service_uuids=scan_uuids,
            )
            logger.debug(
                "Scan completed in %.2f seconds", time.monotonic() - scan_start
            )

            devices = _parse_scan_response(
                response, whitelist_address=target_identifier
            )
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

        if not devices and target_identifier:
            logger.debug(
                "Scan found no devices, trying fallback to already-connected devices"
            )
            connected_coro = self.connected_strategy.discover(
                target_identifier, BLEConfig.BLE_SCAN_TIMEOUT
            )
            try:
                fallback = client.async_await(
                    connected_coro, timeout=BLEConfig.BLE_SCAN_TIMEOUT
                )
                devices.extend(fallback)
            except (SystemExit, KeyboardInterrupt):
                raise
            except Exception as e:  # pragma: no cover - best effort logging
                logger.warning("Connected device fallback failed: %s", e, exc_info=True)
                # Attempt to close the coroutine if async_await failed before scheduling
                with contextlib.suppress(Exception):
                    connected_coro.close()

        return devices

    def close(self) -> None:
        """
        Close the manager's persistent discovery client and clear the internal reference.
        
        If a persistent client exists, it is closed and the manager's internal reference is set to None; if no client is present, this method does nothing.
        """
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None

    def __del__(self) -> None:
        """
        Drop the internal client reference during garbage collection.

        Destructor cleanup is intentionally minimal: avoid performing BLE I/O
        (for example, `client.close()`) during interpreter finalization.
        Explicit lifecycle owners should call `close()`.
        """
        # Use setattr only; __init__ may not have completed in exceptional paths.
        if hasattr(self, "_client"):
            self._client = None