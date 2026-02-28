"""BLE device discovery strategies."""

import asyncio
import contextlib
import inspect
import re
import threading
import time
from collections.abc import Awaitable
from types import TracebackType
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

_BLE_ADDRESS_KEY_RE = re.compile(r"^[0-9a-f]{12}$")
_BLE_ADDRESS_SHAPE_RE = re.compile(
    r"^[0-9A-Fa-f]{12}$|^[0-9A-Fa-f]{2}(?:[:\-_ ][0-9A-Fa-f]{2}){5}$"
)
_DISCOVERY_FACTORY_LOG_KWARG = "log_if_no_address"
_UNEXPECTED_KEYWORD_FRAGMENT = "unexpected keyword argument"
_PENDING_DISCOVERY_CLOSE_TASKS: set[asyncio.Task[None]] = set()


def _is_unexpected_keyword_error(exc: TypeError, kwarg_name: str) -> bool:
    """Return True when a TypeError clearly indicates an unsupported keyword arg."""
    message = str(exc)
    return _UNEXPECTED_KEYWORD_FRAGMENT in message and f"'{kwarg_name}'" in message


def _looks_like_ble_address(identifier: str) -> bool:
    """Return True when an identifier is plausibly a BLE address string."""
    stripped = identifier.strip()
    if not stripped:
        return False
    return bool(_BLE_ADDRESS_SHAPE_RE.fullmatch(stripped))


def _finalize_discovery_close_task(task: asyncio.Task[None]) -> None:
    """Release retained close tasks and log failures in best-effort cleanup paths."""
    _PENDING_DISCOVERY_CLOSE_TASKS.discard(task)
    with contextlib.suppress(asyncio.CancelledError):
        close_exc = task.exception()
        if close_exc is not None:
            logger.debug(
                "Async close/disconnect failed for discarded discovery client.",
                exc_info=(type(close_exc), close_exc, close_exc.__traceback__),
            )


async def _await_close_result(close_result: Awaitable[Any]) -> None:
    """Await a best-effort discovery-client close/disconnect result."""
    await close_result


def _close_discovery_client_best_effort(client: Any) -> None:
    """Best-effort close of a discarded discovery client.

    Prefers ``close()`` and falls back to ``disconnect()`` when available. If
    the close call returns an awaitable, it is awaited when no event loop is
    running in this thread, or scheduled on the current running loop.
    """
    close = getattr(client, "close", None)
    if not callable(close):
        close = getattr(client, "disconnect", None)
    if not callable(close):
        return

    try:
        close_result = close()
    except Exception:  # noqa: BLE001 - best effort cleanup path
        logger.debug(
            "Failed to close discarded discovery client of type %s.",
            type(client).__name__,
            exc_info=True,
        )
        return

    if not inspect.isawaitable(close_result):
        return

    awaitable_result = cast(Awaitable[Any], close_result)
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(_await_close_result(awaitable_result))
        except Exception:  # noqa: BLE001 - best effort cleanup path
            logger.debug(
                "Failed to await close/disconnect for discarded discovery client of type %s.",
                type(client).__name__,
                exc_info=True,
            )
    else:
        try:
            close_task = running_loop.create_task(_await_close_result(awaitable_result))
            _PENDING_DISCOVERY_CLOSE_TASKS.add(close_task)
            close_task.add_done_callback(_finalize_discovery_close_task)
        except Exception:  # noqa: BLE001 - best effort cleanup path
            logger.debug(
                "Failed to schedule async close/disconnect for discarded discovery client of type %s.",
                type(client).__name__,
                exc_info=True,
            )
            close_awaitable = getattr(awaitable_result, "close", None)
            if callable(close_awaitable):
                with contextlib.suppress(Exception):
                    close_awaitable()


class DiscoveryClientError(Exception):
    """An exception class for BLE discovery client errors."""

    @classmethod
    def factory_returned_none(
        cls, resolved_factory: Callable[..., Any]
    ) -> "DiscoveryClientError":
        """Build an error for factories that returned None."""
        return cls(
            f"Discovery client factory returned None. Factory: {resolved_factory!r}"
        )

    @classmethod
    def invalid_client(
        cls,
        resolved_factory: Callable[..., Any],
        returned_type: type[Any],
        missing_attrs: list[str],
    ) -> "DiscoveryClientError":
        """Build an error for factories returning an invalid duck-typed client."""
        return cls(
            "Discovery client factory returned invalid type. "
            f"Factory: {resolved_factory!r}, returned type: {returned_type!r}, "
            f"missing required attributes: {missing_attrs}"
        )


def _normalize_device_name_for_matching(name: str | None) -> str | None:
    """Normalize a Bluetooth device name for tolerant comparisons.

    Strips leading/trailing whitespace and applies casefolding; preserves
    punctuation. If the input is None or reduces to an empty string after
    normalization, returns `None`.

    Parameters
    ----------
    name : str | None
        Raw device name.

    Returns
    -------
    str | None
        The normalized name (`name.strip().casefold()`), or `None` if input is
        None or empty after normalization.
    """
    if name is None:
        return None
    normalized_name = name.strip().casefold()
    return normalized_name or None


def _filter_devices_for_target_identifier(
    devices: list[BLEDevice], target_identifier: str
) -> list[BLEDevice]:
    """Select BLEDevice objects matching a user-supplied address or name.

    Matching precedence:
    1) Exact normalized address match.
    2) Exact name match (case-sensitive).
    3) Normalized name match (casefolded and stripped) only when exactly one
       candidate matches.

    Parameters
    ----------
    devices : list[BLEDevice]
        Candidate devices to search.
    target_identifier : str
        User-supplied address or device name to match.

    Returns
    -------
    list[BLEDevice]
        Devices that match according to the precedence rules. Returns an empty
        list when no match is found or when multiple devices match by normalized
        name (ambiguous).
    """
    target_key: str | None = None
    if _looks_like_ble_address(target_identifier):
        target_key = sanitize_address(target_identifier)
    if target_key and _BLE_ADDRESS_KEY_RE.fullmatch(target_key):
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
    (`strip().casefold()`) only if unique. Otherwise, devices advertising
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

    if has_whitelist:
        return _filter_devices_for_target_identifier(
            target_candidates, cast(str, target_identifier)
        )
    return devices


class DiscoveryManager:
    """Orchestrates BLE device scanning."""

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        """Initialize a DiscoveryManager that orchestrates BLE scanning.

        Parameters
        ----------
        client_factory : Callable[..., Any] | None
            Optional factory to construct BLE client instances; primarily for
            testing or to override the default BLE client implementation. If
            provided, the factory should return a BLEClient-like object or None.
        """
        # Allow test overrides via meshtastic.ble_interface monkeypatch (backwards compatibility)
        self.client_factory = client_factory
        self._client: Any | None = None
        self._client_lock = threading.RLock()

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
            Unexpected non-DBus exceptions are handled internally and
            result in an empty list.

        Raises
        ------
        BleakDBusError
            If a DBus/BlueZ error occurs during scanning; this error is propagated to the caller.
        DiscoveryClientError
            If the discovery client factory returns None.
        DiscoveryClientError
            If the discovery client factory returns an invalid type.
        """
        stale_clients: list[Any] = []
        invalid_client_error: DiscoveryClientError | None = None
        with self._client_lock:

            def _discard_cached_client() -> None:
                cached_client = self._client
                if cached_client is not None:
                    stale_clients.append(cached_client)
                    self._client = None

            # Only discard the client if it was previously connected and has since
            # disconnected. A discovery-only client (never connected) should be reused.
            if self._client is not None:
                bleak = getattr(self._client, "bleak_client", None)
                if bleak is not None:
                    is_connected_method = getattr(self._client, "isConnected", None)
                    if not callable(is_connected_method):
                        logger.debug(
                            "Cached discovery client lacks isConnected(); discarding client."
                        )
                        _discard_cached_client()
                    else:
                        is_connected = cast(Any, is_connected_method)
                        try:
                            if not is_connected():  # pylint: disable=not-callable
                                _discard_cached_client()
                        except (
                            Exception
                        ):  # noqa: BLE001 - defensive path for flaky clients
                            logger.debug(
                                "Cached discovery client isConnected() failed; discarding client.",
                                exc_info=True,
                            )
                            _discard_cached_client()
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
                # Prefer signature-based compatibility checks so real factory
                # TypeErrors are not misclassified as kwarg mismatch.
                try:
                    signature = inspect.signature(resolved_factory)
                except (TypeError, ValueError):
                    signature = None

                accepts_log_kwarg = False
                if signature is not None:
                    accepts_log_kwarg = (
                        _DISCOVERY_FACTORY_LOG_KWARG in signature.parameters
                    ) or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    )

                if signature is None:
                    try:
                        self._client = resolved_factory(log_if_no_address=False)
                    except TypeError as exc:
                        if _is_unexpected_keyword_error(
                            exc, _DISCOVERY_FACTORY_LOG_KWARG
                        ):
                            logger.debug(
                                "Discovery client factory rejected log_if_no_address kwarg; retrying without it: %s",
                                exc,
                                exc_info=True,
                            )
                            self._client = resolved_factory()
                        else:
                            raise
                elif accepts_log_kwarg:
                    self._client = resolved_factory(log_if_no_address=False)
                else:
                    self._client = resolved_factory()
                # Validate factory returned a valid client (duck typing for testability)
                if self._client is None:
                    raise DiscoveryClientError.factory_returned_none(resolved_factory)
                # Accept BLEClient instances or any object with the required interface
                # (duck typing allows test fixtures while still catching errors)
                if not isinstance(self._client, BLEClient):
                    # Duck-typed discovery clients must support scan operations:
                    # _discover() is the only required method for device scanning.
                    required_callables = ("_discover",)
                    missing = [
                        a
                        for a in required_callables
                        if not callable(getattr(self._client, a, None))
                    ]
                    if missing:
                        invalid_client = self._client
                        _discard_cached_client()
                        invalid_client_error = DiscoveryClientError.invalid_client(
                            resolved_factory,
                            type(invalid_client),
                            missing,
                        )

            client = self._client

        seen_stale_ids: set[int] = set()
        for stale_client in stale_clients:
            stale_id = id(stale_client)
            if stale_id in seen_stale_ids:
                continue
            seen_stale_ids.add(stale_id)
            _close_discovery_client_best_effort(stale_client)

        if invalid_client_error is not None:
            raise invalid_client_error
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
            discover_kwargs: dict[str, Any] = {
                "timeout": BLEConfig.BLE_SCAN_TIMEOUT,
                "return_adv": True,
            }
            if not target_identifier:
                discover_kwargs["service_uuids"] = [SERVICE_UUID]

            response = client._discover(**discover_kwargs)
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
        except Exception as e:  # pragma: no cover  # noqa: BLE001
            # Defensive last resort to keep discovery best-effort
            logger.warning(
                "Unexpected error during device discovery: %s", e, exc_info=True
            )
            devices = []

        return devices

    def close(self) -> None:
        """Close the manager's persistent discovery client and clear the internal reference.

        If a persistent client exists with a close method, it is closed and the
        manager's internal reference is set to None; if no client is present,
        this method does nothing.
        """
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            _close_discovery_client_best_effort(client)

    def __enter__(self) -> "DiscoveryManager":
        """Return self for context-manager usage."""
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the manager on context-manager exit.

        If an exception occurred within the with-block, any exception raised by
        close() is logged and suppressed so the original exception propagates.
        If no with-block exception occurred, close() exceptions propagate normally.
        """
        try:
            self.close()
        except Exception:
            if _exc_type is not None:
                logger.warning(
                    "close() failed while unwinding an existing exception.",
                    exc_info=True,
                )
            else:
                raise

    def __del__(self) -> None:
        """Drop the internal client reference during garbage collection.

        Destructor cleanup is intentionally minimal: avoid performing BLE I/O
        (for example, `client.close()`) during interpreter finalization.
        Explicit lifecycle owners should call `close()`.
        """
        # Only set attributes; avoid calling methods as __init__ may not have completed.
        with contextlib.suppress(Exception):
            if hasattr(self, "_client_lock"):
                with self._client_lock:
                    self._client = None
            elif hasattr(self, "_client"):
                self._client = None
