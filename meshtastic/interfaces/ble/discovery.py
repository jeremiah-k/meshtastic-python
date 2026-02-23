"""BLE device discovery strategies."""

import time
from typing import Any, Callable, cast

from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import (
    SERVICE_UUID,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.utils import (
    resolve_ble_module,
    sanitize_address,
)


def _normalize_device_name_for_matching(name: str | None) -> str | None:
    """Normalize a Bluetooth device name for tolerant comparisons.

    Strips leading/trailing whitespace and applies casefolding; preserves punctuation. If the input is None or reduces to an empty string after normalization, returns `None`.

    Parameters
    ----------
    name : str | None
        Raw device name.

    Returns
    -------
    str | None
        The normalized name (`name.strip().casefold()`), or `None` if input is None or empty after normalization.
    """
    if name is None:
        return None
    normalized_name = name.strip().casefold()
    return normalized_name or None


def _filter_devices_for_target_identifier(
    devices: list[BLEDevice], target_identifier: str
) -> list[BLEDevice]:
    """Selects BLEDevice objects matching a user-supplied address or name using deterministic precedence.

    Matching precedence:
    1) Exact normalized address match.
    2) Exact name match (case-sensitive).
    3) Normalized name match (casefolded and stripped) only when exactly one candidate matches.

    Parameters
    ----------
    devices : list[BLEDevice]
        Candidate devices to search.
    target_identifier : str
        User-supplied address or device name to match.

    Returns
    -------
    list[BLEDevice]
        Devices that match according to the precedence rules. Returns an empty list when no match is found or when multiple devices match by normalized name (ambiguous).
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
    response: Any, whitelist_address: str | None = None
) -> list[BLEDevice]:
    """Convert BleakScanner.discover(return_adv=True) output into BLEDevice objects.

    When `whitelist_address` is provided, matches are selected using
    address-first and name-aware precedence:
    1) exact normalized address key, 2) exact name, 3) normalized name
    (`casefold().strip()`) only if unique. Otherwise, devices advertising
    `SERVICE_UUID` are returned.

    Parameters
    ----------
    response : Any
        The value returned by BleakScanner.discover(return_adv=True); expected to be a dict mapping identifiers to (device, adv) tuples.
    whitelist_address : str | None
        Raw address or device name target. (Default value = None)

    Returns
    -------
    list[BLEDevice]
        Devices matching the target (targeted mode) or devices
        advertising SERVICE_UUID (broad scan mode).
    """
    devices: list[BLEDevice] = []
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
    target_candidates: list[BLEDevice] = []
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
            # Targeted scans intentionally skip SERVICE_UUID filtering here.
            # _filter_devices_for_target_identifier() handles direct-address/name
            # matching, and DiscoveryManager uses scan_uuids=None for that flow.
            target_candidates.append(device)
        elif has_service:
            devices.append(device)

    if has_whitelist and target_identifier is not None:
        return _filter_devices_for_target_identifier(
            target_candidates, target_identifier
        )
    return devices


class DiscoveryManager:
    """Orchestrates BLE device scanning."""

    def __init__(self, client_factory: Callable[..., BLEClient] | None = None) -> None:
        """Initialize a DiscoveryManager that orchestrates BLE scanning.

        Parameters
        ----------
        client_factory : Callable[..., BLEClient] | None
            Optional factory to construct BLE client instances; primarily for testing or to override the default BLE client implementation. If provided, the factory should return a BLEClient-like object or None.
        """
        # Allow test overrides via meshtastic.ble_interface monkeypatch (backwards compatibility)
        self.client_factory = client_factory
        self._client: BLEClient | None = None

    def _discover_devices(self, address: str | None) -> list[BLEDevice]:
        """Discover BLE devices advertising the configured service UUID.

        Parameters
        ----------
        address : str | None
            Bluetooth address or device name to filter results; when provided the scan is run broadly to ensure the target is found.

        Returns
        -------
        list[BLEDevice]
            Devices found by the scan, possibly an empty list.

        Raises
        ------
        BleakDBusError
            If a DBus/BlueZ error occurs during scanning; this error is propagated to the caller.
        RuntimeError
            If the discovery client factory returns None.
        RuntimeError
            If the discovery client factory returns an invalid type.
        Exception
            For other unexpected errors during device discovery.
        """
        # Only discard the client if it was previously connected and has since
        # disconnected. A discovery-only client (never connected) should be reused.
        if self._client is not None:
            bleak = getattr(self._client, "bleak_client", None)
            if bleak is not None and not self._client.isConnected():
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
            # for custom factories that don't accept this kwarg. This broad
            # TypeError catch can hide internal factory TypeErrors, but we keep
            # it for compatibility with older/custom factory signatures.
            try:
                self._client = resolved_factory(log_if_no_address=False)
            except TypeError as exc:
                logger.debug(
                    "Discovery client factory rejected log_if_no_address kwarg; retrying without it: %s",
                    exc,
                    exc_info=True,
                )
                self._client = resolved_factory()
            # Validate factory returned a valid client (duck typing for testability)
            if self._client is None:
                raise RuntimeError(
                    f"Discovery client factory returned None. Factory: {resolved_factory!r}"
                )
            # Accept BLEClient instances or any object with the required interface
            # (duck typing allows test fixtures while still catching errors)
            if not isinstance(self._client, BLEClient):
                # Duck-typed discovery clients must support scan operations:
                # _discover() and context manager protocol (__enter__/__exit__).
                required_attrs = (
                    "_discover",
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
        devices: list[BLEDevice] = []
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

            response = client._discover(
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

        return devices

    def close(self) -> None:
        """Close the manager's persistent discovery client and clear the internal reference.

        If a persistent client exists, it is closed and the manager's internal reference is set to None; if no client is present, this method does nothing.
        """
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None

    def __del__(self) -> None:
        """Drop the internal client reference during garbage collection.

        Destructor cleanup is intentionally minimal: avoid performing BLE I/O
        (for example, `client.close()`) during interpreter finalization.
        Explicit lifecycle owners should call `close()`.
        """
        # Use setattr only; __init__ may not have completed in exceptional paths.
        if hasattr(self, "_client"):
            self._client = None
