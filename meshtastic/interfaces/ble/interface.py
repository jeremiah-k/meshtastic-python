"""Main BLE interface class.

Concurrency model summary
-------------------------
`BLEInterface` intentionally centralizes connection/disconnect, receive-loop, and
recovery control because these paths share state transitions and lock-sensitive
invariants.

When multiple locks are required, acquire in this order:
1. per-address locks from gating (`_addr_lock_context`)
2. `_connect_lock`
3. `_management_lock`
4. `_state_lock`
5. `_disconnect_lock`

`_REGISTRY_LOCK` is only held for short registry updates and must never be held
while waiting to acquire a per-address lock.

Threading model summary
-----------------------
- Receive thread: owns inbound packet reads and disconnect escalation.
- Event/reconnect workers: coordinate wakeups and policy-driven reconnect.
- Main thread: issues lifecycle operations (`connect()`, `close()`), which are
  idempotent and synchronized through the shared state manager lock.
"""

import atexit
import contextlib
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from threading import Event
from typing import IO, Any, TypeVar, cast

from bleak import BleakClient as BleakRootClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError

from meshtastic import publishingThread
from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.compatibility_service import BLECompatibilityEventService
from meshtastic.interfaces.ble.connection import (
    ClientManager,
    ConnectionOrchestrator,
    ConnectionValidator,
)
from meshtastic.interfaces.ble.constants import (
    BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
    CONNECTION_ERROR_EMPTY_ADDRESS,
    CONNECTION_ERROR_LOST_OWNERSHIP,
    ERROR_ADDRESS_RESOLUTION_FAILED,
    ERROR_CONNECTION_FAILED,
    ERROR_CONNECTION_SUPPRESSED,
    ERROR_DISCOVERY_MANAGER_UNAVAILABLE,
    ERROR_INTERFACE_CLOSING,
    ERROR_MANAGEMENT_ADDRESS_EMPTY,
    ERROR_MANAGEMENT_ADDRESS_REQUIRED,
    ERROR_MANAGEMENT_CONNECTING,
    ERROR_MANAGEMENT_TARGET_CHANGED,
    ERROR_MULTIPLE_DEVICES,
    ERROR_MULTIPLE_DEVICES_DISCOVERY,
    ERROR_NO_CLIENT_ESTABLISHED,
    ERROR_NO_PERIPHERALS_FOUND,
    ERROR_TIMEOUT,
    ERROR_TRUST_ADDRESS_NOT_RESOLVED,
    ERROR_WRITING_BLE,
    FROMNUM_UUID,
    GATT_IO_TIMEOUT,
    LEGACY_LOGRADIO_UUID,
    LOGRADIO_UUID,
    MALFORMED_NOTIFICATION_THRESHOLD,
    NOTIFICATION_START_TIMEOUT,
    RECEIVE_THREAD_JOIN_TIMEOUT,
    SERVICE_UUID,
    TORADIO_UUID,
    UNREACHABLE_ADDRESSED_DEVICES_MSG,
    BLEConfig,
    logger,
)
from meshtastic.interfaces.ble.coordination import ThreadCoordinator, ThreadLike
from meshtastic.interfaces.ble.discovery import (
    DiscoveryManager,
    _looks_like_ble_address,
    _parse_scan_response,
)
from meshtastic.interfaces.ble.errors import BLEErrorHandler, DecodeError
from meshtastic.interfaces.ble.gating import (
    _addr_key,
    _addr_lock_context,
    _clear_connecting,
    _is_currently_connected_elsewhere,
    _mark_connected,
    _mark_connecting,
    _mark_disconnected,
)
from meshtastic.interfaces.ble.lifecycle_service import BLELifecycleService
from meshtastic.interfaces.ble.management_service import (
    BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS as _BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
    BLEManagementCommandsService,
)
from meshtastic.interfaces.ble.notifications import NotificationManager
from meshtastic.interfaces.ble.policies import RetryPolicy
from meshtastic.interfaces.ble.receive_service import BLEReceiveRecoveryService
from meshtastic.interfaces.ble.reconnection import ReconnectScheduler
from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState
from meshtastic.interfaces.ble.utils import _sleep, sanitize_address, with_timeout
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import mesh_pb2

T = TypeVar("T")
_NOTIFY_ACQUIRED_FRAGMENT = "notify acquired"
_MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS: float = 30.0
_MANAGEMENT_CONNECT_WAIT_POLL_SECONDS: float = 0.5
_MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS: float = 30.0
_TRUST_COMMAND_OUTPUT_MAX_CHARS: int = 200
_TRUST_HEX_BLOB_RE = re.compile(r"\b[0-9A-Fa-f]{16,}\b")
_TRUST_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")
ERROR_PAIR_ON_CONNECT_BOOL: str = "pair_on_connect must be a bool."
ERROR_PAIR_BOOL: str = "pair must be a bool when provided."
ERROR_RETRY_POLICY_MISSING_SHOULD_RETRY: str = (
    "Retry policy missing should_retry/_should_retry"
)
ERROR_RETRY_POLICY_MISSING_GET_DELAY: str = "Retry policy missing get_delay/_get_delay"


class BLEInterface(MeshInterface):
    """MeshInterface using BLE to connect to Meshtastic devices.

    This class provides a complete BLE interface for Meshtastic communication,
    handling connection management, packet transmission/reception, error recovery,
    and automatic reconnection. It extends MeshInterface with BLE-specific
    functionality while maintaining API compatibility.

    Notes
    -----
    - Key features:
      - Automatic connection management and recovery.
      - Thread-safe operations with centralized thread coordination.
      - Unified error handling and logging.
      - Configurable timeouts and retry behavior.
      - Support for both legacy and modern BLE characteristics.
      - Comprehensive state management.
    - Internal architecture:
      - `BLEStateManager`: Centralized connection state machine with shared locking.
      - `ThreadCoordinator`: Thread/event lifecycle and coordination utilities.
      - `BLEErrorHandler`: Standardized error handling patterns.
      - `NotificationManager`: Tracks active notifications for reconnect-safe resubscription.
      - `DiscoveryManager`: Scans for Meshtastic BLE devices with address normalization.
      - `ConnectionValidator`: Enforces connection preconditions.
      - `ClientManager`: Owns BLEClient lifecycle and cleanup operations.
      - `ConnectionOrchestrator`: Coordinates connection establishment.
      - `ReconnectScheduler` / `ReconnectWorker`: Policy-driven reconnect attempts.

    Note: This interface requires appropriate Bluetooth permissions and may
    need platform-specific setup for BLE operations.
    """

    class BLEError(MeshInterface.MeshInterfaceError):
        """An exception class for BLE errors."""

    def __init__(
        self,
        address: str | None = None,
        noProto: bool = False,
        debugOut: IO[str] | Callable[[str], Any] | None = None,
        noNodes: bool = False,
        timeout: float = 300.0,
        *,
        auto_reconnect: bool = False,
        pair_on_connect: bool = False,
    ) -> None:
        """Initialize the BLEInterface, configure background threads, and attempt an initial connection to a Meshtastic BLE device.

        If an address or device name is provided, attempt to connect to that device; otherwise discovery may select any available Meshtastic device. Registers an atexit handler to ensure an orderly disconnect on process exit.

        Parameters
        ----------
        address : str | None
            BLE address or device name to connect to; if None, discovery may select any compatible device.
        auto_reconnect : bool
            If True, schedule automatic reconnection after unexpected disconnects. (Default value = False)
        pair_on_connect : bool
            If True, request BLE pairing as part of each connect attempt. This maps
            to Bleak's `BleakClient(..., pair=True)` behavior. (Default value = False)
        noProto : bool
            If True, skip protobuf protocol initialization. (Default value = False)
        debugOut : IO[str] | Callable[[str], Any] | None
            Optional stream or callable for debug output; if None, uses sys.stderr. (Default value = None)
        noNodes : bool
            If True, skip node database initialization. (Default value = False)
        timeout : float
            Connection timeout in seconds. (Default value = 300.0)

        Raises
        ------
        BLEInterface.BLEError
            If the initial connection or configuration fails.
        """

        # Thread safety and state management
        # Unified state-based lock system replacing multiple locks and boolean flags
        #
        # Lock Ordering (to prevent deadlocks):
        #     - Never hold _REGISTRY_LOCK while waiting on a per-address lock.
        #     - For interface-level coordination, acquire in this order:
        #       1. Per-address locks (_ADDR_LOCKS in gating.py, via _addr_lock_context)
        #       2. Interface connect lock (_connect_lock)
        #       3. Interface management lock (_management_lock)
        #       4. Interface state lock (_state_lock)
        #       5. Interface disconnect lock (_disconnect_lock)
        #
        # EXCEPTION: In _handle_disconnect, _disconnect_lock is acquired FIRST
        # (non-blocking) before _state_lock. This intentional inversion enables
        # early-return optimization for concurrent disconnect callbacks without
        # blocking on state_lock. Safe because _disconnect_lock uses non-blocking
        # acquire. If other code needs both locks, it acquires state_lock first,
        # then disconnect_lock, and _handle_disconnect's non-blocking acquire will
        # simply fail and return early.
        #
        # _connect_lock Purpose:
        #     Serializes connection attempts within a single interface instance.
        #     While _addr_lock_context provides process-wide serialization for the
        #     same address, _connect_lock ensures that within this interface,
        #     only one connection attempt can be in the critical section at a time.
        #     This prevents race conditions when checking existing_client and
        #     managing the connection state machine.
        self._state_manager = BLEStateManager()  # Centralized state tracking
        self._state_lock = (
            self._state_manager.lock
        )  # `lock` returns the shared RLock instance
        self._connect_lock = threading.RLock()  # Serializes connection attempts
        self._management_lock = (
            threading.RLock()
        )  # Serializes pair/unpair/trust vs. close()
        self._management_idle_condition = threading.Condition(self._management_lock)
        self._management_inflight = 0  # Tracks end-to-end management operations.
        self._disconnect_lock = threading.Lock()  # Serializes disconnect handling
        self._closed: bool = (
            False  # Tracks completion of shutdown for idempotent close()
        )
        self._exit_handler: Any | None = None
        self.address = address
        self._last_connection_request: str | None = sanitize_address(address)
        self.auto_reconnect = auto_reconnect
        if not isinstance(pair_on_connect, bool):
            raise self.BLEError(ERROR_PAIR_ON_CONNECT_BOOL)
        self.pair_on_connect = pair_on_connect
        self._disconnect_notified = False  # Prevents duplicate disconnect events
        self._client_publish_pending = False  # Hide provisional clients.
        self._client_replacement_pending = False
        self._last_disconnect_source: str = (
            ""  # Set by _handle_disconnect on each disconnect
        )
        self._connection_alias_key: str | None = None  # Track alias for cleanup
        self._prior_publish_was_reconnect = False
        self._last_connect_pair_override: bool | None = None
        self._last_connect_timeout_override: float | None = None

        # Error handling infrastructure
        self.error_handler = BLEErrorHandler()

        # Thread management infrastructure
        self.thread_coordinator = ThreadCoordinator()
        self._notification_manager = NotificationManager()
        self._discovery_manager: DiscoveryManager | None = DiscoveryManager()
        self._connection_validator = ConnectionValidator(
            self._state_manager, self._state_lock, self.BLEError
        )
        self._client_manager = ClientManager(
            self._state_manager,
            self._state_lock,
            self.thread_coordinator,
            self.error_handler,
        )
        self._connection_orchestrator = ConnectionOrchestrator(
            self,
            self._connection_validator,
            self._client_manager,
            self._discovery_manager,
            self._state_manager,
            self._state_lock,
            self.thread_coordinator,
        )
        self._reconnect_scheduler = ReconnectScheduler(
            self._state_manager,
            self._state_lock,
            self.thread_coordinator,
            self,
        )

        # Event coordination for reconnection and read operations
        self._read_trigger = self.thread_coordinator._create_event(
            "read_trigger"
        )  # Signals when data is available to read
        self._reconnected_event = self.thread_coordinator._create_event(
            "reconnected_event"
        )  # Signals when reconnection occurred
        self._shutdown_event = self.thread_coordinator._create_event("shutdown_event")
        # Whether FROMNUM notifications were successfully registered for the
        # active connection. When false, receive loop falls back to periodic
        # FROMRADIO polling.
        self._fromnum_notify_enabled = False
        self._malformed_notification_count = 0  # Tracks corrupted packets for threshold
        self._malformed_notification_lock = threading.Lock()
        self._ever_connected = (
            False  # Track first successful connection to tune logging
        )
        # Recovery throttling to prevent tight crash→spawn loops
        self._receive_recovery_attempts = 0
        self._last_recovery_time = 0.0  # monotonic clock

        # Initialize parent interface
        super().__init__(
            debugOut=debugOut, noProto=noProto, noNodes=noNodes, timeout=timeout
        )

        # Initialize retry counter for transient read errors
        # Policies are immutable presets; cache instances to avoid churn in hot loops.
        self._empty_read_policy = RetryPolicy.empty_read()
        self._transient_read_policy = RetryPolicy.transient_error()
        self._read_retry_count = 0
        self._last_empty_read_warning = 0.0
        self._suppressed_empty_read_warnings = 0

        self.client: BLEClient | None = None

        # Start background receive thread for inbound packet processing
        logger.debug("Threads starting")
        with self._state_lock:
            self._want_receive = True
        self._receiveThread: ThreadLike | None = None
        self._start_receive_thread(name="BLEReceive")
        logger.debug("Threads running")
        try:
            logger.debug("BLE connecting to: %s", address if address else "any")
            self.connect(address, connect_timeout=timeout)
            logger.debug("BLE connected")

            logger.debug("Mesh configure starting")
            self._start_config()
            if not self.noProto:
                self._wait_connected(timeout=timeout)
                self.waitForConfig()

            # FROMNUM notification is set in _register_notifications

            # We MUST run atexit (if we can) because otherwise (at least on linux) the BLE device is not disconnected
            # and future connection attempts will fail.  (BlueZ kinda sucks)
            # Note: the on disconnected callback will call our self.close which will make us nicely wait for threads to exit
            self._exit_handler = atexit.register(self.close)
        except (SystemExit, KeyboardInterrupt):  # pylint: disable=W0706
            self.close()
            raise
        except MeshInterface.MeshInterfaceError:
            # BLEInterface.BLEError and any other MeshInterfaceError subclass raised
            # by connect() (e.g., ERROR_CONNECTION_SUPPRESSED) need cleanup before
            # propagating.  Re-raise without wrapping to preserve the original message.
            self.close()
            raise
        except (BleakError, BLEClient.BLEError, OSError, RuntimeError) as e:
            self.close()
            raise BLEInterface.BLEError(ERROR_CONNECTION_FAILED.format(e)) from e

    def __repr__(self) -> str:
        """Compact textual representation of the BLEInterface showing its address and any active non-default flags.

        Includes the `address`, `debugOut` when set, and the boolean flags
        `noProto`, `noNodes`, `auto_reconnect`, and `pair_on_connect` only when
        they differ from their defaults.

        Returns
        -------
        str
            String representation like "BLEInterface(address='AA:BB:CC:DD:EE:FF', debugOut='...', noProto=True)".
        """
        parts = [f"address={self.address!r}"]
        if self.debugOut is not None:
            parts.append(f"debugOut={self.debugOut!r}")
        if self.noProto:
            parts.append("noProto=True")
        if self.noNodes:
            parts.append("noNodes=True")
        if self.auto_reconnect:
            parts.append("auto_reconnect=True")
        if self.pair_on_connect:
            parts.append("pair_on_connect=True")
        return f"BLEInterface({', '.join(parts)})"

    def _set_receive_wanted(self, want_receive: bool) -> None:
        """Request or clear running of the background receive loop and record that intent under the interface state lock.

        Parameters
        ----------
        want_receive : bool
            True to request the receive loop to run, False to stop it.
        """
        BLELifecycleService._set_receive_wanted(self, want_receive)

    def _should_run_receive_loop(self) -> bool:
        """Return whether the receive loop should run.

        Returns
        -------
        bool
            `True` if the receive loop is desired and the interface is not closed, `False` otherwise.
        """
        return BLELifecycleService._should_run_receive_loop(self)

    def _start_receive_thread(self, *, name: str, reset_recovery: bool = True) -> None:
        """Create and start the background receive thread, updating `_receiveThread`.

        Parameters
        ----------
        name : str
            Thread name to assign (for diagnostics/logging).
        reset_recovery : bool
            If True, reset the recovery attempt counter after
            successful thread start; if False, preserve the counter for recovery
            backoff tracking. Defaults to True.
        """
        BLELifecycleService._start_receive_thread(
            self, name=name, reset_recovery=reset_recovery
        )

    @staticmethod
    def _sorted_address_keys(*keys: str | None) -> list[str]:
        """Return a deterministically ordered list of unique, non-empty address keys.

        Parameters
        ----------
        *keys : str | None
            One or more address key strings; `None` or empty values are ignored.

        Returns
        -------
        list[str]
            Unique, non-empty address keys sorted in deterministic order.
        """
        return sorted({key for key in keys if key})

    def _lock_ordered_address_keys(
        self, ordered_keys: list[str]
    ) -> contextlib.ExitStack:
        """Acquire per-address locks in a deterministic order and return an ExitStack that holds them.

        The returned ExitStack contains the acquired per-address lock contexts; keep the stack open (or use it as a context manager) for the duration the locks are required so they remain held and are released when the stack is closed.

        Parameters
        ----------
        ordered_keys : list[str]
            Address keys in the exact order locks should be acquired.

        Returns
        -------
        contextlib.ExitStack
            An active ExitStack with the acquired address-lock contexts.

        Raises
        ------
        RuntimeError
            If acquiring any of the address locks fails.
        """
        stack = contextlib.ExitStack()
        try:
            for key in ordered_keys:
                addr_lock = stack.enter_context(_addr_lock_context(key))
                stack.enter_context(addr_lock)
        except BaseException:
            stack.close()
            raise
        return stack

    def _mark_address_keys_connected(self, *keys: str | None) -> None:
        """Mark the given address registry keys as connected for this interface.

        Ignores None or empty strings; for each non-empty key the registry is updated to record this interface as the owner.

        Parameters
        ----------
        *keys : str | None
            One or more address registry keys to mark as connected.
        """
        ordered_keys = self._sorted_address_keys(*keys)
        if not ordered_keys:
            return
        # Do not hold _REGISTRY_LOCK while waiting on per-address locks.
        # _mark_connected() acquires _REGISTRY_LOCK internally after the
        # address locks are held, which avoids registry<->address deadlocks
        # with concurrent connect/disconnect paths.
        with self._lock_ordered_address_keys(ordered_keys):
            for key in ordered_keys:
                _mark_connected(key, owner=self)

    def _mark_address_keys_connecting(self, *keys: str | None) -> None:
        """Record provisional address claims for an in-flight connect result."""
        for key in self._sorted_address_keys(*keys):
            _mark_connecting(key, owner=self)

    def _clear_address_keys_connecting(self, *keys: str | None) -> None:
        """Clear provisional address claims for an in-flight connect result."""
        for key in self._sorted_address_keys(*keys):
            _clear_connecting(key, owner=self)

    def _mark_address_keys_disconnected(self, *keys: str | None) -> None:
        """Mark one or more address registry keys as disconnected.

        Ignores None or empty keys. Updates the global address registry so the interface is no longer recorded as the owner of each provided key; this is performed under the registry and per-address locks to ensure consistent state.

        Parameters
        ----------
        *keys : str | None
            One or more address key strings to mark disconnected; None or empty values are skipped.
        """
        ordered_keys = self._sorted_address_keys(*keys)
        if not ordered_keys:
            return
        # Do not hold _REGISTRY_LOCK while waiting on per-address locks.
        # _mark_disconnected() acquires _REGISTRY_LOCK internally after the
        # address locks are held, which avoids registry<->address deadlocks
        # with concurrent connect/disconnect paths.
        with self._lock_ordered_address_keys(ordered_keys):
            for key in ordered_keys:
                _mark_disconnected(key, owner=self)

    def _handle_disconnect(
        self,
        source: str,
        client: BLEClient | None = None,
        bleak_client: BleakRootClient | None = None,
    ) -> bool:
        """Handle a BLE client disconnection by updating state and either scheduling an automatic reconnect or initiating shutdown.

        Parameters
        ----------
        source : str
            Short tag identifying where the disconnect originated (for logging).
        client : BLEClient | None
            The BLEClient instance associated with the disconnect, if available. (Default value = None)
        bleak_client : BleakRootClient | None
            The underlying Bleak client instance, if available. (Default value = None)

        Returns
        -------
        bool
            `True` if the interface remains active and will attempt auto-reconnect, `False` if shutdown has begun.
        """
        if not self._disconnect_lock.acquire(blocking=False):
            # Another disconnect handler is active; this is expected during concurrent
            # disconnect callbacks. The active handler will process the disconnect,
            # including deciding whether to keep running and schedule reconnect.
            # Mirror current shutdown/reconnect intent so callers can stop when
            # reconnect is not expected.
            logger.debug(
                "Disconnect from %s skipped: another disconnect handler is active.",
                source,
            )
            with self._state_lock:
                return (
                    self.auto_reconnect
                    and not self._closed
                    and not self._state_manager._is_closing
                )
        disconnect_lock_released = False
        target_client = client
        previous_client: BLEClient | None = None
        client_at_start: BLEClient | None = None
        should_reconnect = False
        should_schedule_reconnect = False
        was_publish_pending = False
        was_replacement_pending = False
        address = "unknown"
        disconnect_keys: list[str] = []
        try:
            # Lock-order invariant: release _disconnect_lock before any operations
            # that compute/acquire address locks (addr_disconnect_key via
            # _sorted_address_keys). disconnect_lock_released guarantees
            # _disconnect_lock is released exactly once even on exceptions.
            # Perform state checks and state mutation atomically under the state lock.
            with self._state_lock:
                current_state = self._state_manager._current_state
                current_client = self.client
                is_closing = self._state_manager._is_closing or self._closed
                was_publish_pending = self._client_publish_pending
                was_replacement_pending = self._client_replacement_pending

                if current_state == ConnectionState.CONNECTING:
                    logger.debug(
                        "Ignoring disconnect from %s while a connection is in progress.",
                        source,
                    )
                    # Early returns inside this try block are safe: the finally
                    # below releases _disconnect_lock when disconnect_lock_released
                    # is still False.
                    return True
                if is_closing:
                    logger.debug("Ignoring disconnect from %s during shutdown.", source)
                    return False

                # Resolve callback source against the currently active client.
                if target_client is None and bleak_client is not None:
                    if (
                        current_client
                        and getattr(current_client, "bleak_client", None)
                        is bleak_client
                    ):
                        target_client = current_client
                    elif current_client is not None:
                        logger.debug("Ignoring stale disconnect from %s.", source)
                        return True

                # Ignore stale disconnect callbacks from non-active clients.
                if (
                    target_client is not None
                    and current_client is not None
                    and target_client is not current_client
                ):
                    logger.debug("Ignoring stale disconnect from %s.", source)
                    return True

                # Prevent duplicate disconnect notifications.
                if self._disconnect_notified:
                    logger.debug("Ignoring duplicate disconnect from %s.", source)
                    return True

                previous_client = current_client
                client_at_start = current_client
                alias_key = self._connection_alias_key
                self.client = None
                self._client_publish_pending = False
                self._client_replacement_pending = False
                self._disconnect_notified = True
                self._connection_alias_key = None
                self._state_manager._transition_to(ConnectionState.DISCONNECTED)
                should_reconnect = self.auto_reconnect
                should_schedule_reconnect = should_reconnect and not self._closed

                if target_client:
                    address = getattr(target_client, "address", repr(target_client))
                elif bleak_client:
                    address = getattr(bleak_client, "address", repr(bleak_client))
                elif previous_client:
                    address = getattr(previous_client, "address", repr(previous_client))

                if should_reconnect:
                    if previous_client:
                        previous_address = getattr(
                            previous_client, "address", self.address
                        )
                        device_key = (
                            _addr_key(previous_address) if previous_address else None
                        )
                        disconnect_keys = self._sorted_address_keys(
                            device_key, alias_key
                        )
                    else:
                        fallback_key = _addr_key(self.address)
                        disconnect_keys = self._sorted_address_keys(
                            fallback_key, alias_key
                        )
                else:
                    # Guard against sentinel "unknown" address - don't pollute registry.
                    # Prefer the previous active client's address because callback metadata can be stale.
                    address_for_registry = (
                        getattr(previous_client, "address", None)
                        if previous_client
                        else (address if address != "unknown" else self.address)
                    )
                    addr_disconnect_key = _addr_key(address_for_registry)
                    disconnect_keys = self._sorted_address_keys(
                        addr_disconnect_key, alias_key
                    )

            # Release the disconnect lock before any address-lock operations.
            self._disconnect_lock.release()
            disconnect_lock_released = True
        finally:
            if not disconnect_lock_released:
                self._disconnect_lock.release()

        skip_side_effects = False
        stale_disconnect_keys: list[str] = []
        with self._state_lock:
            active_client = self.client
            if active_client is not None and active_client is not client_at_start:
                active_keys = set(
                    self._sorted_address_keys(
                        _addr_key(getattr(active_client, "address", None)),
                        self._connection_alias_key,
                    )
                )
                stale_disconnect_keys = [
                    key for key in disconnect_keys if key not in active_keys
                ]
                skip_side_effects = True

        def _close_previous_client_async() -> None:
            if previous_client:
                # Keep cleanup behavior consistent between reconnect paths.
                close_thread = self.thread_coordinator._create_thread(
                    target=self._client_manager_safe_close_client,
                    args=(previous_client,),
                    name="BLEClientClose",
                    daemon=True,
                )
                self.thread_coordinator._start_thread(close_thread)

        if skip_side_effects:
            if stale_disconnect_keys:
                self._mark_address_keys_disconnected(*stale_disconnect_keys)
            _close_previous_client_async()
            logger.debug(
                "Skipping stale disconnect side-effects from %s: newer client already active.",
                source,
            )
            return True

        logger.debug("BLE client %s disconnected (source: %s).", address, source)
        # Expose the most recent disconnect source for external listeners that
        # only receive the generic meshtastic.connection.lost event.
        self._last_disconnect_source = f"ble.{source}"

        if disconnect_keys:
            self._mark_address_keys_disconnected(*disconnect_keys)

        _close_previous_client_async()
        if not was_publish_pending or was_replacement_pending:
            self._disconnected()
        else:
            logger.debug(
                "Skipping public disconnect event for provisional session from %s.",
                source,
            )

        if should_reconnect:
            # Event coordination for reconnection (only if not closed)
            if should_schedule_reconnect:
                self.thread_coordinator._clear_events(
                    "read_trigger", "reconnected_event"
                )
                self._schedule_auto_reconnect()
            return True

        logger.debug("Auto-reconnect disabled, staying disconnected.")
        return False

    def _on_ble_disconnect(self, client: BleakRootClient) -> None:
        """Handle a Bleak client disconnect callback.

        Parameters
        ----------
        client : BleakRootClient
            The Bleak client instance that disconnected.
        """
        BLELifecycleService._on_ble_disconnect(self, client)

    def _schedule_auto_reconnect(self) -> None:
        """Schedule repeated automatic reconnection attempts until a connection is established or shutdown begins.

        Does nothing if automatic reconnection is disabled or the interface is closing or already closed.
        """
        BLELifecycleService._schedule_auto_reconnect(self)

    def _handle_malformed_fromnum(self, reason: str, exc_info: bool = False) -> None:
        """Track malformed FROMNUM notifications and log occurrences; emit a warning when a configured threshold is reached.

        Parameters
        ----------
        reason : str
            Description of why the notification was considered malformed.
        exc_info : bool
            If True, include exception traceback information in the log. (Default value = False)
        """
        with self._malformed_notification_lock:
            self._malformed_notification_count += 1
            logger.debug("%s", reason, exc_info=exc_info)
            if self._malformed_notification_count >= MALFORMED_NOTIFICATION_THRESHOLD:
                logger.warning(
                    "Received %d malformed FROMNUM notifications. Check BLE connection stability.",
                    self._malformed_notification_count,
                )
                self._malformed_notification_count = 0

    def _from_num_handler(self, _: Any, b: bytes | bytearray) -> None:
        """Process a FROMNUM characteristic notification and wake the receive loop.

        Parses a 4-byte little-endian unsigned 32-bit integer from the notification payload. On successful parse the internal malformed-notification counter is reset and the parsed value is logged. On parse failure or unexpected length the malformed-notification counter is incremented via _handle_malformed_fromnum and a warning may be emitted when a threshold is reached. Always triggers the thread coordinator's "read_trigger" event to wake the read loop.

        Parameters
        ----------
        _ : Any
            Unused sender parameter provided by the BLE library.
        b : bytes | bytearray
            Notification payload expected to contain a 4-byte little-endian unsigned 32-bit integer.
        """
        try:
            if len(b) != 4:
                self._handle_malformed_fromnum(
                    f"FROMNUM notify has unexpected length {len(b)}; ignoring"
                )
                return
            from_num = struct.unpack("<I", b)[0]
            logger.debug("FROMNUM notify: %d", from_num)
            # Successful parse: reset malformed counter
            with self._malformed_notification_lock:
                self._malformed_notification_count = 0
        except (struct.error, ValueError):
            self._handle_malformed_fromnum(
                "Malformed FROMNUM notify; ignoring", exc_info=True
            )
            return
        finally:
            self.thread_coordinator._set_event("read_trigger")

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEInterface callback.
    # Keep callable without deprecation warning.
    def from_num_handler(self, sender: Any, b: bytes | bytearray) -> None:
        """Backward-compatible wrapper for the legacy FROMNUM callback name.

        Parameters
        ----------
        sender : Any
            Notification sender/handle value provided by Bleak.
        b : bytes | bytearray
            FROMNUM payload.
        """
        self._from_num_handler(sender, b)

    def _register_notifications(self, client: BLEClient) -> None:
        """Register BLE characteristic notification handlers on the given BLE client.

        Registers optional legacy and modern log notification handlers (failures to start these are logged and ignored)
        and registers the critical FROMNUM notification handler for incoming packets. FROMNUM registration failures are
        propagated unless BlueZ reports an acquired-notify conflict, in which case this method falls back to periodic
        FROMRADIO polling for compatibility.
        All handlers are wrapped to route exceptions to the interface's error handler.

        Parameters
        ----------
        client : BLEClient
            Connected BLE client on which to subscribe notifications.
        """

        def _safe_call(
            handler: Callable[[Any, Any], None],
            sender: Any,
            data: Any,
            error_msg: str,
        ) -> None:
            """Run a notification handler and forward any exception it raises to the interface's error handler.

            Parameters
            ----------
            handler : Callable[[Any, Any], None]
                Function to invoke with (sender, data).
            sender : Any
                Origin of the notification passed to the handler.
            data : Any
                Notification payload (e.g., bytes, bytearray, or parsed object).
            error_msg : str
                Message given to the error handler if the handler raises an exception.
            """
            self.error_handler.safe_execute(
                lambda: handler(sender, data),
                error_msg=error_msg,
            )

        def _safe_legacy_handler(sender: Any, data: bytes | bytearray) -> None:
            """Invoke the legacy log-radio notification handler for a BLE notification and suppress any exceptions raised by the handler.

            Parameters
            ----------
            sender : Any
                The notification source (characteristic or client) that produced the payload.
            data : bytes | bytearray
                Raw notification payload (bytes or bytearray) delivered by the BLE characteristic.
            """
            _safe_call(
                self._legacy_log_radio_handler,
                sender,
                data,
                "Error in legacy log notification handler",
            )

        def _safe_log_handler(sender: Any, data: bytes | bytearray) -> None:
            """Forward a BLE log-characteristic notification to the configured log handler and record an error if the handler raises.

            Parameters
            ----------
            sender : Any
                Notification sender (characteristic or client); may be unused by the handler.
            data : bytes | bytearray
                Raw notification payload from the BLE device.
            """
            _safe_call(
                self._log_radio_handler,
                sender,
                data,
                "Error in log notification handler",
            )

        def _safe_from_num_handler(sender: Any, data: bytes) -> None:
            """Safely invoke the FROMNUM notification handler, forwarding the sender and raw payload and reporting any exceptions raised.

            Parameters
            ----------
            sender : Any
                Identifier for the notification source (e.g., client or characteristic).
            data : bytes
                Raw FROMNUM characteristic payload.
            """
            _safe_call(
                self._from_num_handler,
                sender,
                data,
                "Error in FROMNUM notification handler",
            )

        def _get_or_create_handler(
            uuid: str, factory: Callable[[], Callable[[Any, Any], None]]
        ) -> Callable[[Any, Any], None]:
            """Return the registered notification handler for a characteristic UUID, creating and subscribing one via the provided factory if none exists.

            Parameters
            ----------
            uuid : str
                Characteristic UUID to look up or register.
            factory : Callable[[], Callable[[Any, Any], None]]
                Factory that returns a handler callable taking (sender, data).

            Returns
            -------
            Callable[[Any, Any], None]
                The existing or newly created notification handler.
            """
            handler = self._notification_manager.get_callback(uuid)
            if handler is None:
                handler = factory()
                self._notification_manager.subscribe(uuid, handler)
            return handler

        def _is_notify_acquired_error(err: BaseException) -> bool:
            """Return True when a notification-start error indicates an acquired notify state."""
            return _NOTIFY_ACQUIRED_FRAGMENT in str(err).casefold()

        # Optional log notifications - failures are non-fatal.
        try:
            if client.has_characteristic(LEGACY_LOGRADIO_UUID):
                legacy_handler = _get_or_create_handler(
                    LEGACY_LOGRADIO_UUID, lambda: _safe_legacy_handler
                )
                client.start_notify(
                    LEGACY_LOGRADIO_UUID,
                    legacy_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
        except (
            BleakError,
            BleakDBusError,
            RuntimeError,
            BLEClient.BLEError,
            self.BLEError,
        ) as e:
            logger.debug(
                "Failed to start optional legacy log notifications for %s: %s",
                LEGACY_LOGRADIO_UUID,
                e,
            )
        try:
            if client.has_characteristic(LOGRADIO_UUID):
                log_handler = _get_or_create_handler(
                    LOGRADIO_UUID, lambda: _safe_log_handler
                )
                client.start_notify(
                    LOGRADIO_UUID,
                    log_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
        except (
            BleakError,
            BleakDBusError,
            RuntimeError,
            BLEClient.BLEError,
            self.BLEError,
        ) as e:
            logger.debug(
                "Failed to start optional log notifications for %s: %s",
                LOGRADIO_UUID,
                e,
            )

        # Critical notification for packet ingress
        from_num_handler = _get_or_create_handler(
            FROMNUM_UUID, lambda: _safe_from_num_handler
        )
        with self._state_lock:
            self._fromnum_notify_enabled = False

        max_attempts = BLEConfig.SERVICE_CHARACTERISTIC_RETRY_COUNT + 1
        for attempt in range(max_attempts):
            try:
                client.start_notify(
                    FROMNUM_UUID,
                    from_num_handler,
                    timeout=NOTIFICATION_START_TIMEOUT,
                )
            except BleakDBusError as e:
                if not _is_notify_acquired_error(e):
                    raise
                logger.debug(
                    "FROMNUM notify already acquired for %s; retrying after best-effort stop_notify (attempt %d/%d)",
                    FROMNUM_UUID,
                    attempt + 1,
                    max_attempts,
                )
                with contextlib.suppress(
                    BleakError,
                    BleakDBusError,
                    RuntimeError,
                    BLEClient.BLEError,
                    self.BLEError,
                ):
                    client.stop_notify(
                        FROMNUM_UUID,
                        timeout=NOTIFICATION_START_TIMEOUT,
                    )
                if attempt + 1 < max_attempts:
                    _sleep(BLEConfig.SERVICE_CHARACTERISTIC_RETRY_DELAY * (attempt + 1))
                    continue
                logger.warning(
                    "Unable to start FROMNUM notifications for %s after %d attempts due to BlueZ 'Notify acquired'; falling back to polling reads.",
                    FROMNUM_UUID,
                    max_attempts,
                )
                return
            else:
                with self._state_lock:
                    self._fromnum_notify_enabled = True
                return

    def _log_radio_handler(self, _: Any, b: bytes | bytearray) -> None:
        """Handle a protobuf LogRecord notification and forward a formatted log line to the instance log handler.

        Parses the notification payload as a mesh_pb2.LogRecord and forwards its message to self._handle_log_line. If the record includes a `source` the message is prefixed with "[source] ". Malformed records are logged and ignored.

        Parameters
        ----------
        _ : Any
            Unused sender/handle value provided by the BLE library.
        b : bytes | bytearray
            Serialized mesh_pb2.LogRecord payload from the BLE notification.
        """
        log_record = mesh_pb2.LogRecord()
        try:
            log_record.ParseFromString(bytes(b))

            message = (
                f"[{log_record.source}] {log_record.message}"
                if log_record.source
                else log_record.message
            )
            self._handle_log_line(message)
        except DecodeError:
            logger.warning("Malformed LogRecord received. Skipping.")

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEInterface callback.
    # Keep callable without deprecation warning.
    async def log_radio_handler(self, sender: Any, b: bytes | bytearray) -> None:
        """Backward-compatible wrapper for the legacy log callback name.

        Historical API in 2.7.7 used an async signature; keep it unchanged.

        Parameters
        ----------
        sender : Any
            Notification sender/handle value provided by Bleak.
        b : bytes | bytearray
            Serialized mesh_pb2.LogRecord payload.
        """
        # Async signature is intentional for 2.7.7 API compatibility.
        # Keep direct in-thread dispatch (no asyncio.to_thread/run_in_executor)
        # so legacy callback ordering/side-effects remain synchronous.
        self._log_radio_handler(sender, b)

    def _legacy_log_radio_handler(self, _: Any, b: bytes | bytearray) -> None:
        """Deliver a legacy UTF-8 log notification payload to the log handler.

        Decodes the notification payload as UTF-8, strips newline characters, and forwards the resulting string to self._handle_log_line. If decoding fails, the payload is ignored and a warning is logged.

        Parameters
        ----------
        _ : Any
            Sender or handle value provided by the BLE library (unused).
        b : bytes | bytearray
            Raw notification payload expected to contain a UTF-8 encoded log line.
        """
        try:
            log_radio = b.decode("utf-8").replace("\n", "")
            self._handle_log_line(log_radio)
        except UnicodeDecodeError:
            logger.warning(
                "Malformed legacy LogRecord received (not valid utf-8). Skipping."
            )

    # COMPAT_STABLE_SHIM (2.7.7): historical public BLEInterface callback.
    # Keep callable without deprecation warning.
    async def legacy_log_radio_handler(self, sender: Any, b: bytes | bytearray) -> None:
        """Backward-compatible wrapper for the legacy log callback name.

        Historical API in 2.7.7 used an async signature; keep it unchanged.

        Parameters
        ----------
        sender : Any
            Notification sender/handle value provided by Bleak.
        b : bytes | bytearray
            Raw UTF-8 legacy log payload.
        """
        self._legacy_log_radio_handler(sender, b)

    @staticmethod
    async def _with_timeout(
        awaitable: Awaitable[T], timeout: float | None, label: str
    ) -> T:
        """Await an awaitable and raise a BLEInterface.BLEError if it does not complete within the given timeout.

        Parameters
        ----------
        awaitable : Awaitable[T]
            The awaitable to await.
        timeout : float | None
            Maximum time in seconds to wait; if None, wait indefinitely.
        label : str
            Short label used in the timeout error message.

        Returns
        -------
        T
            The value produced by the awaited awaitable.

        Raises
        ------
        BLEInterface.BLEError
            If the awaitable does not finish before the timeout elapses.
        """
        return await with_timeout(
            awaitable,
            timeout,
            label,
            timeout_error_factory=lambda timeout_label, timeout_seconds: BLEInterface.BLEError(
                ERROR_TIMEOUT.format(timeout_label, timeout_seconds)
            ),
        )

    @staticmethod
    def scan() -> list[BLEDevice]:
        """Scan for BLE devices advertising the Meshtastic service UUID.

        Performs a timed scan and returns discovered devices that advertise the Meshtastic service.
        Returns an empty list if no matching devices are found or if the scan fails for non-DBus reasons.

        Returns
        -------
        list[BLEDevice]
            Discovered BLEDevice instances advertising the Meshtastic service.

        Raises
        ------
        BleakDBusError
            If a DBus-level error occurs during scanning (propagated to callers).
        """
        with BLEClient(log_if_no_address=False) as client:
            logger.debug(
                "Scanning for BLE devices (takes %.0f seconds)...",
                BLEConfig.BLE_SCAN_TIMEOUT,
            )
            try:
                response = client._discover(
                    timeout=BLEConfig.BLE_SCAN_TIMEOUT,
                    return_adv=True,
                    service_uuids=[SERVICE_UUID],
                )
                return _parse_scan_response(response)
            except BleakDBusError:
                # Propagate DBus-level failures so callers can back off appropriately
                raise
            except (BleakError, RuntimeError) as e:
                logger.warning("Device scan failed: %s", e, exc_info=True)
                return []
            except (
                Exception  # noqa: BLE001
            ) as e:  # pragma: no cover - defensive last resort
                logger.warning(
                    "Unexpected error during device scan: %s", e, exc_info=True
                )
                return []

    def findDevice(self, address: str | None) -> BLEDevice:
        """Locate a Meshtastic BLEDevice by address or device name.

        Parameters
        ----------
        address : str | None
            A device address or name to match. Separators (':', '-', '_', and spaces) are ignored when matching. If None, any discovered Meshtastic device may be returned.

        Returns
        -------
        BLEDevice
            The matched BLE device.

        Raises
        ------
        BLEInterface.BLEError
            If no Meshtastic devices are found, if multiple matching devices are found when an `address` was provided, if the discovery manager is unavailable, or if a synthetic device cannot be created from the provided address.
        """

        target = address or getattr(self, "address", None)
        sanitized = sanitize_address(target)

        # Pass raw target to discovery; the discovery matcher handles address
        # normalization internally and uses the raw identifier for name matching
        # to correctly match device names containing separators (_, -, spaces, :)
        addressed_devices = self._discover_devices(target)

        if len(addressed_devices) == 0:
            if target:
                if not _looks_like_ble_address(target):
                    raise self.BLEError(ERROR_NO_PERIPHERALS_FOUND)
                logger.warning(
                    "No peripherals found for %s via scan; attempting direct address connect",
                    target,
                )
                if not sanitized:
                    raise self.BLEError(ERROR_ADDRESS_RESOLUTION_FAILED)
                # Create a synthetic BLEDevice for direct address connection.
                # This allows the connection logic to attempt direct connect without
                # verification, supporting pre-bonded devices (e.g., via bluetoothctl).
                logger.debug(
                    "Creating synthetic BLEDevice for direct connect: address=%s, sanitized=%s",
                    target,
                    sanitized,
                )
                # Use the backend-usable raw address as the BLEDevice identity.
                # `sanitized` remains for registry/discovery key normalization.
                return BLEDevice(target, target, {})
            raise self.BLEError(ERROR_NO_PERIPHERALS_FOUND)
        if len(addressed_devices) == 1:
            return addressed_devices[0]

        def _format_device_list(devices: list[BLEDevice]) -> str:
            return "\n".join(f"- {d.name or 'Unknown'} ({d.address})" for d in devices)

        if address and len(addressed_devices) > 1:
            # Build a list of found devices for the error message
            device_list = _format_device_list(addressed_devices)
            raise self.BLEError(ERROR_MULTIPLE_DEVICES.format(address, device_list))
        if len(addressed_devices) > 1:
            device_list = _format_device_list(addressed_devices)
            raise self.BLEError(ERROR_MULTIPLE_DEVICES_DISCOVERY.format(device_list))
        raise AssertionError(UNREACHABLE_ADDRESSED_DEVICES_MSG)

    # COMPAT_STABLE_SHIM: historical public BLEInterface API.
    # Keep callable without deprecation warning.
    def find_device(self, address: str | None) -> BLEDevice:
        """Compatibility wrapper for legacy snake_case callers; delegates to findDevice().

        Parameters
        ----------
        address : str | None
            Bluetooth address or device identifier to resolve; if None, discovery is used to select a device.

        Returns
        -------
        BLEDevice
            The resolved BLEDevice matching the address or discovered selection.
        """
        return self.findDevice(address)

    @staticmethod
    def _extract_client_address(client: BLEClient | None) -> str | None:
        """Return the best-known address from a BLE client instance."""
        if client is None:
            return None
        bleak_client = getattr(client, "bleak_client", None)
        bleak_address = getattr(bleak_client, "address", None)
        return cast(str | None, bleak_address or getattr(client, "address", None))

    def _resolve_target_address_for_management(self, address: str | None) -> str:
        """Resolve a management target to a concrete BLE address.

        This helper is used by `pair()`, `unpair()`, and `trust()` to
        resolve explicit addresses or device-name identifiers into a concrete
        address. When no active device is connected, management operations
        require an explicit target and will not discover an arbitrary device.

        Parameters
        ----------
        address : str | None
            Target address, device name, or None to resolve from the active
            connection when possible.

        Returns
        -------
        str
            Resolved BLE address.

        Raises
        ------
        BLEInterface.BLEError
            If no active/explicit management target is available or if the
            explicit target cannot be resolved to a concrete BLE address.
        """
        requested_identifier = address if address is not None else self.address
        if address is not None and sanitize_address(address) is None:
            raise self.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)
        if address is None and requested_identifier is not None:
            normalized_bound_identifier = sanitize_address(requested_identifier)
            if normalized_bound_identifier is None:
                raise self.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)
        if address is None:
            with self._state_lock:
                current_client = self.client
            if current_client is not None and current_client.isConnected():
                current_address = self._extract_client_address(current_client)
                if current_address:
                    return current_address
            if requested_identifier is None:
                raise self.BLEError(ERROR_MANAGEMENT_ADDRESS_REQUIRED)
        normalized_request = sanitize_address(requested_identifier)
        existing_client = self._get_existing_client_if_valid(normalized_request)
        existing_client_address = self._extract_client_address(existing_client)
        if existing_client_address:
            return existing_client_address
        if requested_identifier and _looks_like_ble_address(requested_identifier):
            return requested_identifier
        return self.findDevice(requested_identifier).address

    def _resolve_target_address_for_connect(
        self, requested_identifier: str | None
    ) -> str | None:
        """Resolve a connect target to a concrete BLE address when possible.

        Parameters
        ----------
        requested_identifier : str | None
            Explicit connect identifier (address/name) or None for implicit
            target selection.

        Returns
        -------
        str | None
            Concrete BLE address when resolution is available, otherwise None.
        """
        if requested_identifier and _looks_like_ble_address(requested_identifier):
            return requested_identifier
        if getattr(self, "_discovery_manager", None) is None:
            # Minimal/unit-test interfaces can intentionally omit discovery.
            return None
        return self.findDevice(requested_identifier).address

    @classmethod
    def _format_bluetoothctl_address(cls, address: str) -> str:
        """Canonicalize a BLE address for bluetoothctl commands."""
        sanitized = sanitize_address(address)
        if sanitized is None or len(sanitized) != 12:
            raise cls.BLEError(ERROR_TRUST_ADDRESS_NOT_RESOLVED.format(address=address))
        octets = [sanitized[index : index + 2].upper() for index in range(0, 12, 2)]
        return ":".join(octets)

    @staticmethod
    def _management_target_gate(target_address: str) -> contextlib.ExitStack:
        """Return a context manager that serializes temporary management work by address.

        Parameters
        ----------
        target_address : str
            BLE address to serialize temporary management operations for.

        Returns
        -------
        contextlib.ExitStack
            Active context stack holding the process-wide per-address gate when
            one applies to `target_address`.
        """
        stack = contextlib.ExitStack()
        target_key = _addr_key(target_address)
        if target_key is not None:
            addr_lock = stack.enter_context(_addr_lock_context(target_key))
            stack.enter_context(addr_lock)
        return stack

    def _get_management_client_if_available(
        self, address: str | None
    ) -> BLEClient | None:
        """Return an active or reusable client for a management operation, if one exists.

        Parameters
        ----------
        address : str | None
            Target address or identifier. When `None`, this helper first checks
            `current_client = self.client` and returns it if connected. When an
            explicit address is provided, the current client path is bypassed;
            the helper normalizes `requested_identifier` via
            `sanitize_address()` and validates only via
            `_get_existing_client_if_valid()`. This asymmetry is intentional so
            explicit address operations are validated against the registry path.

        Returns
        -------
        BLEClient | None
            Connected or reusable client for the requested target, or None when
            management work requires a temporary client.
        """
        requested_identifier = address if address is not None else self.address
        with self._state_lock:
            current_client = (
                self.client
                if address is None
                and not getattr(self, "_client_publish_pending", False)
                else None
            )
        if current_client is not None and current_client.isConnected():
            return current_client
        normalized_request = sanitize_address(requested_identifier)
        return self._get_existing_client_if_valid(normalized_request)

    def _get_management_client_for_target(
        self,
        target_address: str,
        *,
        prefer_current_client: bool,
    ) -> BLEClient | None:
        """Return a reusable management client only when it matches the target address.

        Parameters
        ----------
        target_address : str
            Concrete BLE target address chosen for the management operation.
        prefer_current_client : bool
            When True, consider the interface's current connected client before
            checking the process-wide client registry.

        Returns
        -------
        BLEClient | None
            Matching active or reusable client for `target_address`, or None if
            a temporary client should be created instead.
        """
        normalized_target = sanitize_address(target_address)
        with self._state_lock:
            current_client = (
                self.client
                if prefer_current_client
                and not getattr(self, "_client_publish_pending", False)
                else None
            )
        if current_client is not None and current_client.isConnected():
            current_address = self._extract_client_address(current_client)
            if sanitize_address(current_address) == normalized_target:
                return current_client
        return self._get_existing_client_if_valid(normalized_target)

    def _get_current_implicit_management_binding_locked(self) -> str | None:
        """Return the current implicit management binding while holding `_state_lock`."""
        current_client = self.client
        if current_client is not None and current_client.isConnected():
            client_address = self._extract_client_address(current_client)
            if client_address is not None:
                return client_address
        return self.address

    def _get_current_implicit_management_address_locked(self) -> str | None:
        """Return the current implicit management target when it is already concrete."""
        current_binding = self._get_current_implicit_management_binding_locked()
        if current_binding and _looks_like_ble_address(current_binding):
            return current_binding
        return None

    def _revalidate_implicit_management_target(
        self,
        expected_target_address: str,
        *,
        expected_binding: str | None = None,
    ) -> None:
        """Abort when an implicit management target changed while waiting on the gate.

        Parameters
        ----------
        expected_target_address : str
            Concrete BLE address selected before entering the per-target
            management gate.

        Raises
        ------
        BLEInterface.BLEError
            If the interface's current implicit management target has changed to
            a different concrete BLE address.
        """
        with self._state_lock:
            current_binding = self._get_current_implicit_management_binding_locked()
        if current_binding is None:
            raise self.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)

        if _looks_like_ble_address(current_binding):
            current_target_address = current_binding
        else:
            if expected_binding is None or sanitize_address(
                current_binding
            ) != sanitize_address(expected_binding):
                raise self.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
            # When there's an active connected client for an implicit target,
            # use its concrete address directly instead of calling findDevice
            # (which would trigger device discovery). The expected_target_address
            # was already resolved from the active client before entering the gate.
            current_target_address = expected_target_address

        if sanitize_address(current_target_address) != sanitize_address(
            expected_target_address
        ):
            raise self.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)

    def _execute_management_command(
        self,
        address: str | None,
        command: Callable[[BLEClient], T],
    ) -> T:
        """Run a BLE management command using an active, existing, or temporary client.

        Parameters
        ----------
        address : str | None
            Target address or identifier. If None, prefer the currently connected
            client for this interface when available.
        command : Callable[[BLEClient], T]
            Command to execute against the selected BLE client.

        Returns
        -------
        T
            Command return value.
        """
        return BLEManagementCommandsService.execute_management_command(
            self,
            address,
            command,
            ble_client_factory=BLEClient,
            connected_elsewhere=_is_currently_connected_elsewhere,
        )

    def _begin_management_operation_locked(self) -> None:
        """Record a management operation while holding `_management_lock`."""
        self._management_inflight += 1

    def _finish_management_operation(self) -> None:
        """
        Mark completion of an in-flight management operation and notify any waiters when no operations remain.

        If the internal in-flight counter is already zero or negative, reset it to zero, notify waiters, and log a warning; otherwise decrement the counter and notify waiters when it reaches zero.
        """
        with self._management_lock:
            if self._management_inflight <= 0:
                logger.warning(
                    "Management operation accounting underflow detected during finish(); resetting inflight count to zero."
                )
                self._management_inflight = 0
                self._management_idle_condition.notify_all()
                return
            self._management_inflight -= 1
            if self._management_inflight == 0:
                self._management_idle_condition.notify_all()

    def _validate_management_await_timeout(self, await_timeout: object) -> float:
        """Validate and return a bounded await timeout for management operations.

        Parameters
        ----------
        await_timeout : object
            Timeout value to validate; must be a finite positive real number.

        Returns
        -------
        float
            The validated timeout as a float.

        Raises
        ------
        BLEInterface.BLEError
            If `await_timeout` is None, a boolean, non-numeric, non-finite,
            zero, or negative.
        """
        return BLEManagementCommandsService.validate_management_await_timeout(
            self, await_timeout
        )

    def _validate_trust_timeout(self, timeout: object) -> float:
        """Validate and return a bounded timeout for `trust()`.

        Parameters
        ----------
        timeout : object
            Timeout value to validate; must be a finite positive real number.

        Returns
        -------
        float
            The validated timeout as a float.

        Raises
        ------
        BLEInterface.BLEError
            If `timeout` is a boolean, non-numeric, non-finite, zero, or
            negative.
        """
        return BLEManagementCommandsService.validate_trust_timeout(self, timeout)

    def _validate_connect_timeout_override(
        self,
        connect_timeout: object,
        *,
        pair_on_connect: bool,
    ) -> None:
        """Validate a connect timeout override before connection orchestration."""
        BLEManagementCommandsService.validate_connect_timeout_override(
            self,
            connect_timeout,
            pair_on_connect=pair_on_connect,
        )

    def pair(
        self,
        address: str | None = None,
        *,
        await_timeout: float = BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
        **kwargs: object,
    ) -> None:
        """Pair with a BLE device either through the active client or a temporary client.

        Parameters
        ----------
        address : str | None
            Target device address or identifier. If None, use the active
            connection target when available; otherwise fall back to the
            interface's bound target. Raises if no active or bound target is
            available.
        await_timeout : float
            Maximum seconds to wait for the pairing coroutine to complete.
            `BLEInterface` requires a finite positive timeout here so shutdown
            cannot block indefinitely behind a management operation. Defaults
            to `BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT`.
        **kwargs : object
            Backend-specific pairing options forwarded to `BLEClient.pair()`.

        Returns
        -------
        None
            Pairing is performed for side effects and does not return a value.
        """
        BLEManagementCommandsService.pair(
            self,
            address,
            await_timeout=await_timeout,
            kwargs=dict(kwargs),
            ble_client_factory=BLEClient,
            connected_elsewhere=_is_currently_connected_elsewhere,
        )

    def unpair(
        self,
        address: str | None = None,
        *,
        await_timeout: float = BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT,
    ) -> None:
        """Unpair from a BLE device through the active or a temporary client.

        Parameters
        ----------
        address : str | None
            Target device address or identifier. If None, use the active
            connection target when available; otherwise fall back to the
            interface's bound target. Raises if no active or bound target is
            available.
        await_timeout : float
            Maximum seconds to wait for the unpair coroutine to complete.
            `BLEInterface` requires a finite positive timeout here so shutdown
            cannot block indefinitely behind a management operation. Defaults
            to `BLECLIENT_MANAGEMENT_AWAIT_TIMEOUT`.

        Returns
        -------
        None
            Unpairing is performed for side effects and does not return a value.
        """
        BLEManagementCommandsService.unpair(
            self,
            address,
            await_timeout=await_timeout,
            ble_client_factory=BLEClient,
            connected_elsewhere=_is_currently_connected_elsewhere,
        )

    def _run_bluetoothctl_trust_command(
        self,
        bluetoothctl_path: str,
        canonical_address: str,
        validated_timeout: float,
    ) -> None:
        """Run `bluetoothctl trust` and translate subprocess failures into BLEError."""
        BLEManagementCommandsService.run_bluetoothctl_trust_command(
            self,
            bluetoothctl_path,
            canonical_address,
            validated_timeout,
            subprocess_module=subprocess,
            trust_hex_blob_re=_TRUST_HEX_BLOB_RE,
            trust_token_re=_TRUST_TOKEN_RE,
            trust_command_output_max_chars=_TRUST_COMMAND_OUTPUT_MAX_CHARS,
        )

    def trust(
        self,
        address: str | None = None,
        *,
        timeout: float = _BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
    ) -> None:
        """Mark a BLE device as trusted via Linux bluetoothctl.

        Parameters
        ----------
        address : str | None
            Target device address or identifier. If None, use the active
            connection target when available; otherwise fall back to the
            interface's bound target.
        timeout : float
            Maximum seconds to wait for the `bluetoothctl` command to
            complete. Must be a finite positive timeout. Defaults to
            `_BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS`.

        Raises
        ------
        BLEError
            If the interface is closing, if the target address is blank or
            unavailable, if not running on Linux, if `bluetoothctl` is not
            found, or if the trust command fails.

        Notes
        -----
        - This helper is Linux-only and requires `bluetoothctl` on PATH.
        - Pairing PIN/passkey handling remains OS-agent managed.
        """
        BLEManagementCommandsService.trust(
            self,
            address,
            timeout=timeout,
            sys_module=sys,
            shutil_module=shutil,
            subprocess_module=subprocess,
            trust_hex_blob_re=_TRUST_HEX_BLOB_RE,
            trust_token_re=_TRUST_TOKEN_RE,
            trust_command_output_max_chars=_TRUST_COMMAND_OUTPUT_MAX_CHARS,
        )

    def _sanitize_address(self, address: str | None) -> str | None:
        """Provide a backward-compatible wrapper that returns a sanitized BLE address or None.

        Parameters
        ----------
        address : str | None
            BLE address to sanitize; if None, returns None.

        Returns
        -------
        str | None
            The sanitized address string, or `None` if the input was `None`.
        """
        return sanitize_address(address)

    def _discover_devices(self, address: str | None) -> list[BLEDevice]:
        """Discover devices via public API with underscore fallback for legacy test doubles."""
        discovery_manager = self._discovery_manager
        if discovery_manager is None:
            raise self.BLEError(ERROR_DISCOVERY_MANAGER_UNAVAILABLE)
        discover_devices = getattr(discovery_manager, "discover_devices", None)
        if callable(discover_devices):
            return cast(list[BLEDevice], discover_devices(address))
        legacy_discover_devices = getattr(discovery_manager, "_discover_devices", None)
        if callable(legacy_discover_devices):
            return cast(list[BLEDevice], legacy_discover_devices(address))
        # Preserve pre-refactor failure mode for misconfigured doubles.
        return discovery_manager.discover_devices(address)

    def _state_manager_is_connected(self) -> bool:
        """Read connected state with fallback for legacy/mocked state managers."""
        is_connected = getattr(self._state_manager, "_is_connected", None)
        if isinstance(is_connected, bool):
            return is_connected
        legacy_is_connected = getattr(self._state_manager, "is_connected", None)
        if isinstance(legacy_is_connected, bool):
            return legacy_is_connected
        # Preserve pre-refactor failure mode for misconfigured doubles.
        return self._state_manager._is_connected

    def _validator_check_existing_client(
        self,
        client: BLEClient,
        normalized_request: str | None,
        last_connection_request: str | None,
    ) -> bool:
        """Check existing-client compatibility with public-first, legacy-fallback lookup."""
        check_existing_client = getattr(
            self._connection_validator, "check_existing_client", None
        )
        if callable(check_existing_client):
            return bool(
                check_existing_client(
                    client,
                    normalized_request,
                    last_connection_request,
                )
            )
        legacy_check_existing_client = getattr(
            self._connection_validator, "_check_existing_client", None
        )
        if callable(legacy_check_existing_client):
            return bool(
                legacy_check_existing_client(
                    client,
                    normalized_request,
                    last_connection_request,
                )
            )
        # Preserve pre-refactor failure mode for misconfigured doubles.
        return bool(
            self._connection_validator.check_existing_client(
                client,
                normalized_request,
                last_connection_request,
            )
        )

    def _establish_connection(
        self,
        address: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> BLEClient:
        """Establish BLE connection via orchestrator public API with legacy fallback."""
        establish_connection = getattr(
            self._connection_orchestrator, "establish_connection", None
        )
        if callable(establish_connection):
            return cast(
                BLEClient,
                establish_connection(
                    address,
                    self.address,
                    self._register_notifications,
                    # Defer _connected() publication until interface ownership
                    # has been committed and verified.
                    lambda: None,
                    self._on_ble_disconnect,
                    pair_on_connect=pair_on_connect,
                    connect_timeout=connect_timeout,
                    emit_connected_side_effects=False,
                ),
            )
        legacy_establish_connection = getattr(
            self._connection_orchestrator, "_establish_connection", None
        )
        if callable(legacy_establish_connection):
            return cast(
                BLEClient,
                legacy_establish_connection(
                    address,
                    self.address,
                    self._register_notifications,
                    lambda: None,
                    self._on_ble_disconnect,
                    pair_on_connect=pair_on_connect,
                    connect_timeout=connect_timeout,
                    emit_connected_side_effects=False,
                ),
            )
        # Preserve pre-refactor failure mode for misconfigured doubles.
        return self._connection_orchestrator.establish_connection(
            address,
            self.address,
            self._register_notifications,
            lambda: None,
            self._on_ble_disconnect,
            pair_on_connect=pair_on_connect,
            connect_timeout=connect_timeout,
            emit_connected_side_effects=False,
        )

    def _client_manager_safe_close_client(self, client: BLEClient) -> None:
        """Close BLE client via public API with underscore fallback for test doubles."""
        safe_close_client = getattr(self._client_manager, "safe_close_client", None)
        if callable(safe_close_client):
            safe_close_client(client)
            return
        legacy_safe_close_client = getattr(self._client_manager, "_safe_close_client", None)
        if callable(legacy_safe_close_client):
            legacy_safe_close_client(client)
            return
        self._client_manager.safe_close_client(client)

    def _client_manager_update_client_reference(
        self, client: BLEClient, previous_client: BLEClient
    ) -> None:
        """Update client reference via public API with underscore fallback for test doubles."""
        update_client_reference = getattr(
            self._client_manager, "update_client_reference", None
        )
        if callable(update_client_reference):
            update_client_reference(client, previous_client)
            return
        legacy_update_client_reference = getattr(
            self._client_manager, "_update_client_reference", None
        )
        if callable(legacy_update_client_reference):
            legacy_update_client_reference(client, previous_client)
            return
        self._client_manager.update_client_reference(client, previous_client)

    @staticmethod
    def _retry_policy_should_retry(policy: object, attempt: int) -> bool:
        """Evaluate retry decision with public-first, underscore-fallback lookup."""
        should_retry = getattr(policy, "should_retry", None)
        if callable(should_retry):
            return bool(should_retry(attempt))
        legacy_should_retry = getattr(policy, "_should_retry", None)
        if callable(legacy_should_retry):
            return bool(legacy_should_retry(attempt))
        raise AttributeError(ERROR_RETRY_POLICY_MISSING_SHOULD_RETRY)

    @staticmethod
    def _retry_policy_get_delay(policy: object, attempt: int) -> float:
        """Read retry delay with public-first, underscore-fallback lookup."""
        get_delay = getattr(policy, "get_delay", None)
        if callable(get_delay):
            return float(get_delay(attempt))
        legacy_get_delay = getattr(policy, "_get_delay", None)
        if callable(legacy_get_delay):
            return float(legacy_get_delay(attempt))
        raise AttributeError(ERROR_RETRY_POLICY_MISSING_GET_DELAY)

    @property
    def _connection_state(self) -> ConnectionState:
        """Get the current BLE connection state.

        Returns
        -------
        ConnectionState
            The current connection state of the interface.
        """
        with self._state_lock:
            return self._state_manager._current_state

    @property
    def _is_connection_connected(self) -> bool:
        """Return whether the interface currently has an active BLE connection.

        Returns
        -------
        bool
            `True` if a BLE connection is active, `False` otherwise.
        """
        with self._state_lock:
            return self._state_manager._is_connected

    @property
    def _is_connection_closing(self) -> bool:
        """Return whether the interface is shutting down or has already closed.

        Returns
        -------
        bool
            True if the interface is closing or closed, False otherwise.
        """
        with self._state_lock:
            return self._state_manager._is_closing or self._closed

    @property
    def _can_initiate_connection(self) -> bool:
        """Return whether the interface may start a new BLE connection.

        Considers the centralized connection state and whether the interface is shutting down.

        Returns
        -------
        bool
            True if a new connection can be initiated, False otherwise.
        """
        with self._state_lock:
            return self._state_manager._can_connect and not self._closed

    # ---------------------------------------------------------------------
    # Connection helper methods (extracted from connect() for readability)
    # ---------------------------------------------------------------------

    def _validate_connection_preconditions(self) -> None:
        """Raise BLEError if the BLE interface is closing or already closed to prevent initiating a new connection.

        Raises
        ------
        BLEError
            with ERROR_INTERFACE_CLOSING when the interface is closing or already closed.
        """
        if self._is_connection_closing:
            raise self.BLEError(ERROR_INTERFACE_CLOSING)

    def _validate_management_preconditions(self) -> None:
        """Raise BLEError if the interface is closing or actively connecting.

        Management operations are side-effecting and should not race a
        connection attempt that is still establishing or replacing the active
        client. We reject them while CONNECTING rather than trying to interleave
        them with connect/reconnect state transitions.

        Raises
        ------
        BLEError
            With ERROR_INTERFACE_CLOSING when shutdown has begun, or
            ERROR_MANAGEMENT_CONNECTING when a connection attempt is in progress.
        """
        self._validate_connection_preconditions()
        with self._state_lock:
            if (
                self._state_manager._current_state == ConnectionState.CONNECTING
                or self._client_publish_pending
            ):
                raise self.BLEError(ERROR_MANAGEMENT_CONNECTING)

    def _should_suppress_duplicate_connect(self, connection_key: str | None) -> bool:
        """Return whether a connect attempt for the given connection key should be suppressed because an active connection for that key exists on a different interface.

        Parameters
        ----------
        connection_key : str | None
            Address or alias key to check; use None for discovery-mode connects.

        Returns
        -------
        bool
            `True` if the connection should be suppressed because the key is connected elsewhere and this interface is not the active connection, `False` otherwise.
        """
        with self._state_lock:
            is_self_connected = self._state_manager._is_connected
        return bool(
            connection_key
            and _is_currently_connected_elsewhere(connection_key, owner=self)
            and not is_self_connected
        )

    def _raise_if_duplicate_connect(self, connection_key: str | None) -> None:
        """Raise BLEError when connect should be suppressed for a duplicate address claim.

        Parameters
        ----------
        connection_key : str | None
            Address registry key for this connect attempt.

        Raises
        ------
        BLEInterface.BLEError
            If this address is currently connected elsewhere.
        """
        if self._should_suppress_duplicate_connect(connection_key):
            logger.info(
                "Suppressing duplicate connect to %s: recently connected elsewhere.",
                connection_key or "unknown",
            )
            raise self.BLEError(ERROR_CONNECTION_SUPPRESSED)

    def _get_existing_client_if_valid(
        self, normalized_request: str | None
    ) -> BLEClient | None:
        """Return the current BLE client if it represents a connected client compatible with the provided request.

        Parameters
        ----------
        normalized_request : str | None
            Sanitized connection request identifier used to validate compatibility with the current client.

        Returns
        -------
        BLEClient | None
            The existing connected client if compatible with `normalized_request`, otherwise `None`.
        """
        with self._state_lock:
            existing_client = self.client
            last_connection_request = self._last_connection_request
            is_connected = (
                self._state_manager_is_connected()
                and not self._disconnect_notified
                and not getattr(self, "_client_publish_pending", False)
            )
        if not is_connected or existing_client is None:
            return None
        if (
            existing_client.isConnected()
            and self._validator_check_existing_client(
                existing_client,
                normalized_request,
                last_connection_request,
            )
        ):
            return existing_client
        return None

    def _establish_and_update_client(
        self,
        address: str | None,
        normalized_request: str | None,
        address_key: str | None,
        *,
        pair_on_connect: bool = False,
        connect_timeout: float | None = None,
    ) -> tuple[BLEClient, str | None, str | None]:
        """Establish a BLE connection through the orchestrator and update the interface's client and address state.

        Establishes a new connection, stores the resulting client and device address under the state lock, updates the last connection request, transfers any previous client references to the new client, and computes address gating keys for the connected device.

        Parameters
        ----------
        address : str | None
            Target BLE address or device name requested for the connection.
        normalized_request : str | None
            Sanitized identifier for the connection request; used to update last connection request.
        address_key : str | None
            Optional registry key representing the requested address for gate tracking.
        pair_on_connect : bool
            If True, enable pairing during connection establishment for the newly
            created BLE client(s). (Default value = False)
        connect_timeout : float | None
            Optional timeout override forwarded to connection orchestration. When
            `None`, the pairing-aware default timeout is used.

        Returns
        -------
        client_and_keys : tuple[BLEClient, str | None, str | None]
            A tuple containing the connected BLE client, the connected device key, and the connection alias key.

        Notes
        -----
        Must be called while holding _connect_lock.
        """
        with self._state_lock:
            # Claim a provisional session before orchestration moves shared
            # state to CONNECTED so disconnect callbacks stay publication-safe.
            self._client_replacement_pending = (
                self.client is not None and not self._client_publish_pending
            )
            self._client_publish_pending = True

        try:
            client = self._establish_connection(
                address,
                pair_on_connect=pair_on_connect,
                connect_timeout=connect_timeout,
            )
        except Exception:
            with self._state_lock:
                self._client_publish_pending = False
                self._client_replacement_pending = False
            raise

        device_address = getattr(
            getattr(client, "bleak_client", None), "address", None
        ) or getattr(client, "address", None)
        previous_client = None
        abort_connect = False
        with self._state_lock:
            if self._closed or self._state_manager._is_closing:
                abort_connect = True
                self._client_publish_pending = False
                self._client_replacement_pending = False
            else:
                previous_client = self.client
                self.address = device_address
                self.client = client
                self._disconnect_notified = False
                self._client_publish_pending = True
                normalized_device_address = sanitize_address(device_address or "")
                if normalized_request is not None:
                    self._last_connection_request = normalized_request
                else:
                    self._last_connection_request = normalized_device_address

        if abort_connect:
            logger.debug(
                "Discarding late BLE connection result during shutdown: %s",
                sanitize_address(device_address or "") or "unknown",
            )
            # Shutdown is already committed, so target restoration is not needed.
            self._client_manager_safe_close_client(client)
            raise self.BLEError(ERROR_INTERFACE_CLOSING)

        if previous_client and previous_client is not client:
            self._client_manager_update_client_reference(client, previous_client)

        connected_device_key = _addr_key(device_address) if device_address else None
        connection_alias_key = (
            address_key
            if connected_device_key
            and address_key
            and address_key != connected_device_key
            else None
        )
        self._read_retry_count = 0

        return client, connected_device_key, connection_alias_key

    def _get_connected_client_status_locked(
        self, client: BLEClient
    ) -> tuple[bool, bool]:
        """Return owned/closing status for a connected client while holding `_state_lock`."""
        is_closing = self._state_manager._is_closing or self._closed
        is_owned = (
            not self._closed
            and self.client is client
            and self._state_manager._is_connected
            and client.isConnected()
        )
        return is_owned, is_closing

    def _get_connected_client_status(self, client: BLEClient) -> tuple[bool, bool]:
        """Return whether this interface owns `client` and whether shutdown began."""
        with self._state_lock:
            return self._get_connected_client_status_locked(client)

    def _has_lost_gate_ownership(self, *keys: str | None) -> bool:
        """Return whether any connected address key is now owned elsewhere."""
        return any(
            key is not None and _is_currently_connected_elsewhere(key, owner=self)
            for key in keys
        )

    def _raise_for_invalidated_connect_result(
        self,
        connected_client: BLEClient,
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        is_closing: bool,
        lost_gate_ownership: bool,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> None:
        """Clean up a stale connect result and raise the appropriate BLEError."""
        stale_keys = self._sorted_address_keys(
            connected_device_key,
            connection_alias_key,
        )
        if not lost_gate_ownership:
            with self._state_lock:
                active_client = self.client
                active_keys = set(
                    self._sorted_address_keys(
                        _addr_key(self._extract_client_address(active_client)),
                        self._connection_alias_key,
                    )
                )
            stale_keys = [key for key in stale_keys if key not in active_keys]
        if stale_keys:
            self._mark_address_keys_disconnected(*stale_keys)
        self._discard_invalidated_connected_client(
            connected_client,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
        )
        if is_closing:
            raise self.BLEError(ERROR_INTERFACE_CLOSING)
        raise self.BLEError(CONNECTION_ERROR_LOST_OWNERSHIP)

    def _verify_and_publish_connected(
        self,
        connected_client: BLEClient,
        connected_device_key: str | None,
        connection_alias_key: str | None,
        *,
        restore_address: str | None,
        restore_last_connection_request: str | None,
    ) -> None:
        """Publish `_connected()` only if ownership is still valid at publish time."""
        lost_gate_ownership = self._has_lost_gate_ownership(
            connected_device_key,
            connection_alias_key,
        )
        publish_connected = False
        publish_candidate = False
        publish_now = False
        publish_committed = False
        prior_ever_connected = False
        is_closing = False
        with self._state_lock:
            still_owned, is_closing = self._get_connected_client_status_locked(
                connected_client
            )
            if still_owned and not lost_gate_ownership:
                prior_ever_connected = self._ever_connected
                publish_connected = True
        if publish_connected:
            still_owned, is_closing = self._get_connected_client_status(
                connected_client
            )
            if still_owned:
                lost_gate_ownership = self._has_lost_gate_ownership(
                    connected_device_key,
                    connection_alias_key,
                )
                if not lost_gate_ownership:
                    with self._state_lock:
                        still_owned, is_closing = (
                            self._get_connected_client_status_locked(connected_client)
                        )
                        if still_owned:
                            publish_candidate = True
        if publish_candidate:
            can_attempt_publish = False
            with self._state_lock:
                still_owned, is_closing = self._get_connected_client_status_locked(
                    connected_client
                )
                if still_owned and not is_closing:
                    can_attempt_publish = True
            if can_attempt_publish:
                lost_gate_ownership = self._has_lost_gate_ownership(
                    connected_device_key,
                    connection_alias_key,
                )
                if not lost_gate_ownership:
                    with self._state_lock:
                        still_owned, is_closing = (
                            self._get_connected_client_status_locked(connected_client)
                        )
                        if still_owned and not is_closing:
                            publish_now = True
        if publish_now:
            should_publish_connected = False
            with self._state_lock:
                still_owned, is_closing = self._get_connected_client_status_locked(
                    connected_client
                )
                if still_owned and not is_closing:
                    if not self._client_publish_pending:
                        # Some direct test harnesses stub connection setup
                        # without setting provisional publish state.
                        self._client_publish_pending = True
                        self._client_replacement_pending = False
                    should_publish_connected = True
            if should_publish_connected:
                try:
                    self._connected()
                except Exception:
                    with self._state_lock:
                        if self.client is connected_client:
                            self._client_publish_pending = False
                            self._client_replacement_pending = False
                    raise
                with self._state_lock:
                    still_owned, is_closing = self._get_connected_client_status_locked(
                        connected_client
                    )
                    if still_owned and not is_closing:
                        self._ever_connected = True
                        self._prior_publish_was_reconnect = prior_ever_connected
                        self._client_publish_pending = False
                        self._client_replacement_pending = False
                        publish_committed = True
                    elif self.client is connected_client:
                        self._client_publish_pending = False
                        self._client_replacement_pending = False
            if publish_committed:
                self._emit_verified_connection_side_effects(connected_client)
                return

        self._raise_for_invalidated_connect_result(
            connected_client,
            connected_device_key,
            connection_alias_key,
            is_closing=is_closing,
            lost_gate_ownership=lost_gate_ownership,
            restore_address=restore_address,
            restore_last_connection_request=restore_last_connection_request,
        )

    def _emit_verified_connection_side_effects(
        self, connected_client: BLEClient
    ) -> None:
        """Emit reconnect signaling/logging only after verified connect publish."""
        coordinator = getattr(self, "thread_coordinator", None)
        if self._prior_publish_was_reconnect and coordinator is not None:
            coordinator._set_event("reconnected_event")
        self._prior_publish_was_reconnect = False
        normalized_device_address = sanitize_address(
            self._extract_client_address(connected_client)
        )
        logger.info(
            "Connection successful to %s",
            normalized_device_address or "unknown",
        )

    def _discard_invalidated_connected_client(
        self,
        client: BLEClient,
        *,
        restore_address: str | None = None,
        restore_last_connection_request: str | None = None,
    ) -> None:
        """Best-effort cleanup for a connected client invalidated before return.

        Parameters
        ----------
        client : BLEClient
            Connected client result that should no longer be published to
            callers because ownership was lost or shutdown began.
        restore_address : str | None
            Target identifier to restore when the connection result is discarded
            before ownership is finalized.
        restore_last_connection_request : str | None
            Normalized request identifier to restore when the connection result
            is discarded before ownership is finalized.
        """
        restored_address = (
            restore_address.strip()
            if restore_address is not None and restore_address.strip()
            else None
        )
        should_reset_state = False
        is_closing = False
        with self._state_lock:
            if self.client is client:
                is_closing = self._state_manager._is_closing or self._closed
                self.client = None
                self._client_publish_pending = False
                self._client_replacement_pending = False
                # The disconnect callback remains registered on `client` until
                # best-effort close completes, so mark this interface as already
                # notified before close() can trigger a stale callback.
                self._disconnect_notified = True
                if not is_closing:
                    self.address = restored_address
                    self._last_connection_request = restore_last_connection_request
                    self._connection_alias_key = None
                    should_reset_state = True
                else:
                    self._last_connection_request = None
            elif self.client is None and self._client_publish_pending:
                # A provisional client can be detached by a concurrent
                # disconnect path before this cleanup runs; ensure callers do
                # not remain stuck in ERROR_MANAGEMENT_CONNECTING.
                self._client_publish_pending = False
                self._client_replacement_pending = False
                is_closing = self._state_manager._is_closing or self._closed
                if not is_closing:
                    self.address = restored_address
                    self._last_connection_request = restore_last_connection_request
                    self._connection_alias_key = None
                    should_reset_state = True
                else:
                    self._last_connection_request = None

        self._client_manager_safe_close_client(client)

        if should_reset_state:
            with self._state_lock:
                self._state_manager._reset_to_disconnected()

    def _finalize_connection_gates(
        self,
        connected_client: BLEClient,
        connected_device_key: str | None,
        connection_alias_key: str | None,
    ) -> None:
        """Finalize post-connection gating by marking the relevant address keys as connected or undoing those marks if the interface closed or the client became stale during the connection process.

        When the provided client is still the active, connected client, this records the connected device and alias keys and persists the alias key on the interface. If the interface closed or the client is no longer current, any provisional gate marks are removed.

        Parameters
        ----------
        connected_client : BLEClient
            The BLE client instance that just completed connecting.
        connected_device_key : str | None
            Deterministic registry key derived from the connected device's address, or `None` if not applicable.
        connection_alias_key : str | None
            Optional alias key used when claiming connection gates, or `None` if not used.
        """
        still_active, is_closing = self._get_connected_client_status(connected_client)

        if still_active:
            self._mark_address_keys_connected(
                connected_device_key, connection_alias_key
            )
            needs_cleanup = False
            with self._state_lock:
                still_active, is_closing = self._get_connected_client_status_locked(
                    connected_client
                )
                if still_active:
                    self._connection_alias_key = connection_alias_key
                else:
                    if is_closing:
                        logger.debug(
                            "Interface closed during connect(), cleaning up gate claim for %s",
                            getattr(connected_client, "address", "unknown"),
                        )
                    else:
                        logger.debug(
                            "Interface lost ownership during connect(), cleaning up gate claim for %s",
                            getattr(connected_client, "address", "unknown"),
                        )
                    self._connection_alias_key = None
                    needs_cleanup = True
            if needs_cleanup:
                self._mark_address_keys_disconnected(
                    connected_device_key, connection_alias_key
                )
        else:
            if is_closing:
                logger.debug(
                    "Skipping connect gate marking during shutdown for stale client result (%s).",
                    getattr(connected_client, "address", "unknown"),
                )
            else:
                logger.debug(
                    "Skipping connect gate marking for client result that lost ownership (%s).",
                    getattr(connected_client, "address", "unknown"),
                )

    def _is_owned_connected_client(self, client: BLEClient) -> bool:
        """Return whether this interface still owns the provided connected client.

        Parameters
        ----------
        client : BLEClient
            Client instance to validate against the interface's current state.

        Returns
        -------
        bool
            `True` when the interface is not closed, still references `client`,
            and the state machine reports CONNECTED.
        """
        is_owned, _ = self._get_connected_client_status(client)
        return is_owned

    # ---------------------------------------------------------------------
    # Main connection method
    # ---------------------------------------------------------------------

    def connect(
        self,
        address: str | None = None,
        *,
        pair: bool | None = None,
        connect_timeout: float | None = None,
    ) -> BLEClient:
        """Connect to a Meshtastic device over BLE by explicit address or by performing device discovery.

        Attempts to establish and return a connected BLE client for the requested device. If an existing compatible client is already connected, that client is returned. The method uses per-address gating to suppress duplicate concurrent connects and updates the interface's stored address and client reference on success. On failure any client opened during the attempt is closed before the error is propagated.

        Parameters
        ----------
        address : str | None
            BLE address or device name to connect to; if None, discovery is used to select a device. (Default value = None)
        pair : bool | None
            If True, request pairing during connect attempts for this call. If None,
            uses `self.pair_on_connect`. (Default value = None)
        connect_timeout : float | None
            Optional timeout override forwarded to BLE client construction and
            connection attempts for this call. When `None`, the pairing-aware
            default timeout is used.

        Returns
        -------
        BLEClient
            The connected BLE client for the selected device.

        Raises
        ------
        BLEInterface.BLEError
            If the interface is closing, if connection ownership is lost before
            the result can be returned, if connection is suppressed due to a
            recent connect elsewhere, or if the connection attempt fails.
        """
        # Fail fast if interface is closing before acquiring any locks
        self._validate_connection_preconditions()

        requested_identifier: str | None = None
        normalized_request: str | None = None
        if pair is not None and not isinstance(pair, bool):
            raise self.BLEError(ERROR_PAIR_BOOL)
        pair_on_connect = self.pair_on_connect if pair is None else pair
        self._validate_connect_timeout_override(
            connect_timeout,
            pair_on_connect=pair_on_connect,
        )
        with self._state_lock:
            self._last_connect_pair_override = pair
            self._last_connect_timeout_override = connect_timeout

        connected_client: BLEClient | None = None
        connected_device_key: str | None = None
        connection_alias_key: str | None = None
        provisional_keys: list[str] = []

        try:
            management_lock = self._management_lock
            management_idle_condition = self._management_idle_condition
            management_wait_started: float | None = None
            while True:
                requested_identifier = address if address is not None else self.address
                normalized_request = sanitize_address(requested_identifier)
                if address is not None and normalized_request is None:
                    raise self.BLEError(CONNECTION_ERROR_EMPTY_ADDRESS)
                # Management commands increment `_management_inflight` under
                # `_connect_lock` and then continue work outside interface
                # locks. Wait for current operations to settle, then re-check
                # once we have `_connect_lock` to avoid the handoff race.
                with management_lock:
                    while self._management_inflight > 0:
                        self._validate_connection_preconditions()
                        if management_wait_started is None:
                            management_wait_started = time.monotonic()
                        elapsed = time.monotonic() - management_wait_started
                        if elapsed >= _MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS:
                            logger.warning(
                                "Timed out waiting %.1fs for %d inflight management operation(s) before connect()",
                                elapsed,
                                self._management_inflight,
                            )
                            raise self.BLEError(ERROR_MANAGEMENT_CONNECTING)
                        remaining = _MANAGEMENT_CONNECT_WAIT_TIMEOUT_SECONDS - elapsed
                        management_idle_condition.wait(
                            timeout=min(
                                _MANAGEMENT_CONNECT_WAIT_POLL_SECONDS, remaining
                            )
                        )
                    # Reset timeout accounting once we are no longer blocked by
                    # in-flight management operations so future waits start fresh.
                    management_wait_started = None

                # Recompute potentially discovery-backed target state after
                # waiting for inflight management operations so retries do not
                # reuse stale resolution results.
                requested_registry_key = (
                    _addr_key(requested_identifier) if requested_identifier else None
                )
                preexisting_client = self._get_existing_client_if_valid(
                    normalized_request
                )
                if preexisting_client:
                    logger.debug("Already connected, skipping connect call.")
                    return preexisting_client
                resolved_connect_target = self._resolve_target_address_for_connect(
                    requested_identifier
                )
                concrete_registry_key = (
                    _addr_key(resolved_connect_target)
                    if resolved_connect_target
                    else None
                )
                connect_target_identifier = (
                    resolved_connect_target
                    if resolved_connect_target is not None
                    else requested_identifier
                )
                reservation_keys = self._sorted_address_keys(
                    requested_registry_key,
                    concrete_registry_key,
                )

                with contextlib.ExitStack() as stack:
                    for reservation_key in reservation_keys:
                        self._raise_if_duplicate_connect(reservation_key)
                    # Acquire per-address locks without holding _REGISTRY_LOCK
                    # to avoid lock-order inversion with paths that hold
                    # address locks and then mark ownership in registry state.
                    for reservation_key in reservation_keys:
                        addr_lock = stack.enter_context(
                            _addr_lock_context(reservation_key)
                        )
                        stack.enter_context(addr_lock)
                    for reservation_key in reservation_keys:
                        # Re-check after lock acquisition to close TOCTOU windows.
                        self._raise_if_duplicate_connect(reservation_key)

                    with self._connect_lock:
                        with management_lock:
                            if self._management_inflight > 0:
                                continue

                        # Re-check closing state inside connect_lock for extra safety
                        self._validate_connection_preconditions()

                        # Check for existing valid client
                        existing_client = self._get_existing_client_if_valid(
                            normalized_request
                        )
                        if existing_client:
                            logger.debug("Already connected, skipping connect call.")
                            return existing_client
                        with self._state_lock:
                            if self._client_publish_pending:
                                raise self.BLEError(ERROR_MANAGEMENT_CONNECTING)

                        # Establish new connection and update state
                        (
                            connected_client,
                            connected_device_key,
                            connection_alias_key,
                        ) = self._establish_and_update_client(
                            connect_target_identifier,
                            normalized_request,
                            requested_registry_key,
                            pair_on_connect=pair_on_connect,
                            connect_timeout=connect_timeout,
                        )
                        provisional_keys = self._sorted_address_keys(
                            requested_registry_key,
                            connected_device_key,
                            connection_alias_key,
                        )
                        self._mark_address_keys_connecting(*provisional_keys)

                # Finalize after the per-address lock scope exits to avoid
                # nested lock-order inversions when gate finalization
                # reacquires address locks.
                if connected_client is None:
                    raise self.BLEError(ERROR_NO_CLIENT_ESTABLISHED)
                self._finalize_connection_gates(
                    connected_client, connected_device_key, connection_alias_key
                )
                self._verify_and_publish_connected(
                    connected_client,
                    connected_device_key,
                    connection_alias_key,
                    restore_address=requested_identifier,
                    restore_last_connection_request=normalized_request,
                )
                return connected_client
        finally:
            self._clear_address_keys_connecting(*provisional_keys)

    def _handle_read_loop_disconnect(
        self, error_message: str, previous_client: BLEClient
    ) -> bool:
        """Return whether the receive loop should continue after a BLE client disconnect.

        Parameters
        ----------
        error_message : str
            Human-readable description of the disconnection cause.
        previous_client : BLEClient
            The BLE client that disconnected and may be closed.

        Returns
        -------
        bool
            `True` if the read loop should continue to allow auto-reconnect, `False` otherwise.
        """
        return BLEReceiveRecoveryService.handle_read_loop_disconnect(
            self, error_message, previous_client
        )

    def _receive_from_radio_impl(self) -> None:
        """Run the main receive loop that reads FROMRADIO packets and delivers them to the packet handler.

        Waits for read or reconnection events, reads payloads from the active BLE client, forwards non-empty payloads to _handle_from_radio, and manages recovery paths (transient retries, disconnect handling, and thread restart) until the interface is closing or the receive loop is stopped.

        Raises
        ------
        Exception
            For unexpected errors in the receive thread that trigger recovery.
        """
        BLEReceiveRecoveryService.receive_from_radio_impl(self)

    def _recover_receive_thread(self, disconnect_reason: str) -> None:
        """Handle receive-thread failure and trigger guarded recovery.

        Parameters
        ----------
        disconnect_reason : str
            Reason string passed to disconnect handling for diagnostics.
        """
        BLEReceiveRecoveryService.recover_receive_thread(self, disconnect_reason)

    def _read_from_radio_with_retries(
        self,
        client: BLEClient,
        *,
        retry_on_empty: bool = True,
    ) -> bytes | None:
        """Read a non-empty payload from the FROMRADIO characteristic, retrying on repeated empty reads.

        Attempts repeated reads with backoff according to the empty-read policy; if a non-empty payload is read the suppressed-empty-read counter is reset. If all attempts return empty, a throttled warning is emitted.

        Parameters
        ----------
        client : BLEClient
            The connected BLE client to read from.
        retry_on_empty : bool
            Whether to retry and back off on empty reads. When False, performs
            a single short-duration poll read using
            ``BLEConfig.RECEIVE_WAIT_TIMEOUT``. (Default value = True)

        Returns
        -------
        bytes | None
            The payload bytes when a non-empty read occurs, or `None` if no non-empty payload was obtained after retries.
        """
        return BLEReceiveRecoveryService.read_from_radio_with_retries(
            self, client, retry_on_empty=retry_on_empty
        )

    def _handle_transient_read_error(self, error: BleakError) -> None:
        """Apply the transient-read retry policy for a BLE read error.

        If the policy allows another retry, increments the internal retry counter and sleeps the configured delay to permit a retry. If retries are exhausted, resets the counter and raises BLEInterface.BLEError(ERROR_READING_BLE).

        Parameters
        ----------
        error : BleakError
            The transient BLE read error that triggered the retry policy.

        Raises
        ------
        BLEInterface.BLEError
            When the retry policy is exhausted and the read should be treated as persistent.
        """
        BLEReceiveRecoveryService.handle_transient_read_error(self, error)

    def _log_empty_read_warning(self) -> None:
        """Emit a throttled warning when repeated empty FROMRADIO BLE reads are observed.

        If the cooldown period has elapsed, log a warning that an empty read retry limit was exceeded and include how many warnings were suppressed during the last cooldown window; otherwise increment the suppressed-warning counter and log a debug message with the current suppressed count and cooldown duration.
        """
        BLEReceiveRecoveryService.log_empty_read_warning(self)

    def _send_to_radio_impl(self, toRadio: mesh_pb2.ToRadio) -> None:
        """Send a protobuf ToRadio message over the TORADIO BLE characteristic.

        No-op if the serialized payload is empty or the interface is closing; on success the receive loop is signaled to process any response.

        Parameters
        ----------
        toRadio : mesh_pb2.ToRadio
            Protobuf message providing SerializeToString() for the outbound payload.

        Raises
        ------
        BLEError
            If the BLE write operation fails.
        """
        b: bytes = toRadio.SerializeToString()
        if not b:
            return

        write_successful = False
        is_disconnect_msg = toRadio.WhichOneof("payload_variant") == "disconnect"
        # Grab the current client under the shared lock, but perform the blocking write outside
        with self._state_lock:
            client = self.client
            publish_pending = self._client_publish_pending

        if publish_pending and not is_disconnect_msg:
            logger.debug(
                "Skipping TORADIO write while connect publication is pending verification."
            )
            return
        if not client or (self._is_connection_closing and not is_disconnect_msg):
            logger.debug(
                "Skipping TORADIO write: no BLE client or interface is closing."
            )
            return

        logger.debug("TORADIO write: %s", b.hex())
        try:
            # Intentional synchronous write-with-response: preserve in-order send
            # semantics and apply backpressure to callers when the device stalls.
            write_started = time.monotonic()
            client.write_gatt_char(
                TORADIO_UUID, b, response=True, timeout=GATT_IO_TIMEOUT
            )
            write_duration = time.monotonic() - write_started
            if write_duration > 1.0:
                logger.debug("Slow TORADIO write completed in %.2fs", write_duration)
            write_successful = True
        except (BleakError, BLEClient.BLEError, RuntimeError, OSError) as e:
            # Log detailed error information and wrap in our interface exception
            logger.debug(
                "Error during write operation: %s",
                type(e).__name__,
                exc_info=True,
            )
            raise self.BLEError(ERROR_WRITING_BLE) from e

        if write_successful:
            # Brief delay to allow write to propagate before triggering read
            _sleep(BLEConfig.SEND_PROPAGATION_DELAY)
            self.thread_coordinator._set_event(
                "read_trigger"
            )  # Wake receive loop to process any response

    def close(self) -> None:
        """Shuts down the BLE interface and releases associated resources.

        This method performs an idempotent, orderly shutdown: it stops background threads and reconnection activity, disconnects and closes any active BLE client, unsubscribes and cleans up notification and discovery managers, unregisters the atexit handler, publishes a final disconnected indication, and waits briefly for pending disconnect-related notifications to flush.
        """
        # Deliberately do not take _connect_lock here. close() must be able to
        # mark shutdown immediately so any in-flight connect/pair timeout path
        # can observe _closed/_is_closing and abort, rather than forcing close()
        # to wait behind a long BLE connect attempt.
        management_wait_timed_out = False
        management_wait_started = time.monotonic()
        with self._management_lock:
            # Use unified state lock
            with self._state_lock:
                if self._closed:
                    logger.debug(
                        "BLEInterface.close called on already closed interface; ignoring"
                    )
                    return
                was_closing = self._state_manager._is_closing
                # Mark closed immediately to prevent overlapping cleanup in concurrent calls
                self._closed = True
                if was_closing:
                    logger.debug(
                        "BLEInterface.close called while another shutdown is in progress; continuing with cleanup"
                    )
                # Transition to DISCONNECTING only if we're not already fully disconnected or mid-disconnect
                if self._state_manager._current_state not in (
                    ConnectionState.DISCONNECTED,
                    ConnectionState.DISCONNECTING,
                ):
                    self._state_manager._transition_to(ConnectionState.DISCONNECTING)
            while self._management_inflight > 0:
                elapsed = time.monotonic() - management_wait_started
                if elapsed >= _MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS:
                    management_wait_timed_out = True
                    logger.warning(
                        "Timed out waiting %.1fs for %d inflight management operation(s) during shutdown",
                        elapsed,
                        self._management_inflight,
                    )
                    break
                remaining = _MANAGEMENT_SHUTDOWN_WAIT_TIMEOUT_SECONDS - elapsed
                self._management_idle_condition.wait(
                    timeout=min(_MANAGEMENT_CONNECT_WAIT_POLL_SECONDS, remaining)
                )

        try:
            # Release lock before calling MeshInterface.close() to avoid deadlock
            # If MeshInterface.close() acquires locks that other paths also acquire, holding state_lock would cause lock inversion
            if self._shutdown_event is not None:
                self._shutdown_event.set()

            discovery_manager = self._discovery_manager
            self._discovery_manager = None
            if discovery_manager is not None:
                # Close discovery early to request graceful cleanup of the
                # persistent discovery client/resources before downstream
                # shutdown steps. This does not guarantee immediate cancellation
                # of all in-flight scan/connect operations.
                self.error_handler.safe_cleanup(
                    discovery_manager.close, "discovery manager close"
                )

            self._set_receive_wanted(want_receive=False)  # Tell the thread we want it to stop
            self.thread_coordinator._wake_waiting_threads(
                "read_trigger", "reconnected_event"
            )  # Wake all waiting threads
            if self._receiveThread:
                if self._receiveThread is threading.current_thread():
                    logger.debug(
                        "close() called from receive thread; skipping self-join"
                    )
                else:
                    self.thread_coordinator._join_thread(
                        self._receiveThread, timeout=RECEIVE_THREAD_JOIN_TIMEOUT
                    )
                    if self._receiveThread.is_alive():
                        logger.warning(
                            "BLE receive thread did not exit within %.1fs",
                            RECEIVE_THREAD_JOIN_TIMEOUT,
                        )
                self._receiveThread = None

            # Close parent interface (stops publishing thread, etc.)
            self.error_handler.safe_execute(
                lambda: MeshInterface.close(self),
                error_msg="Error closing mesh interface",
            )

            if self._exit_handler:
                atexit.unregister(self._exit_handler)
                self._exit_handler = None

            # Use unified state lock
            with self._state_lock:
                client = self.client
                publish_pending = self._client_publish_pending
                # Don't close client if it was already replaced (race condition)
                # Only close if it's still the current client
                if client is not None:
                    self.client = None
            client_address = self._extract_client_address(client)
            # Only close the client if we were the one who replaced it
            # If it was replaced by a reconnection, the new connection path will clean it up
            if client is not None:
                gate_context = (
                    self._management_target_gate(client_address)
                    if client_address is not None
                    and not management_wait_timed_out
                    and not publish_pending
                    else contextlib.nullcontext()
                )
                with gate_context:
                    self._notification_manager.unsubscribe_all(
                        client, timeout=NOTIFICATION_START_TIMEOUT
                    )
                    self._disconnect_and_close_client(client)
            self._notification_manager.cleanup_all()

            # Use unified state lock
            # Send disconnected indicator if not already notified
            notify = False
            with self._state_lock:
                if self._client_publish_pending:
                    replacement_pending = self._client_replacement_pending
                    self._client_publish_pending = False
                    self._client_replacement_pending = False
                    if replacement_pending and not self._disconnect_notified:
                        self._disconnect_notified = True
                        notify = True
                    else:
                        self._disconnect_notified = True
                elif self._client_replacement_pending:
                    self._client_replacement_pending = False
                    if not self._disconnect_notified:
                        self._disconnect_notified = True
                        notify = True
                elif not self._disconnect_notified:
                    self._disconnect_notified = True
                    notify = True

            if notify:
                self._disconnected()  # send the disconnected indicator up to clients
                self._wait_for_disconnect_notifications()

            # Clean up thread coordinator
            self.thread_coordinator._cleanup()
        finally:
            # Use unified state lock
            with self._state_lock:
                # Record final state as DISCONNECTED for observers; instance remains closed.
                # Safe re-entrant call: _state_lock and _state_manager.lock share the
                # same RLock instance set during state manager initialization.
                self._state_manager._transition_to(ConnectionState.DISCONNECTED)
                alias_key = self._connection_alias_key
                self._connection_alias_key = None
            close_key = _addr_key(self.address)
            self._mark_address_keys_disconnected(close_key, alias_key)

    def _wait_for_disconnect_notifications(self, timeout: float | None = None) -> None:
        """Wait up to timeout seconds for the publishing thread to flush pending publish callbacks.

        Requests a flush on the publishing thread and waits for a flush event. If the wait times out and the publishing thread is still alive, a debug message is logged; if the publishing thread is not running when the timeout elapses, the publish queue is drained synchronously on the current thread. Exceptions raised while requesting the flush are caught and logged by the error handler.

        Parameters
        ----------
        timeout : float | None
            Maximum seconds to wait for the publish queue to flush. If `None`, uses `DISCONNECT_TIMEOUT_SECONDS`. (Default value = None)
        """
        BLECompatibilityEventService.wait_for_disconnect_notifications(
            self,
            timeout,
            publishing_thread=publishingThread,
        )

    def _disconnect_and_close_client(self, client: BLEClient) -> None:
        """Ensure the given BLE client is disconnected and its resources are released.

        Parameters
        ----------
        client : BLEClient
            BLE client to disconnect and close; operation is idempotent and safe to call on already-closed clients.
        """
        BLELifecycleService._disconnect_and_close_client(self, client)

    def _drain_publish_queue(self, flush_event: Event) -> None:
        """Drain and run pending publish callbacks on the current thread until the queue is empty or the provided event is set.

        Each callback is executed via the interface's error handler; exceptions raised by callbacks are caught and logged so draining continues.

        Parameters
        ----------
        flush_event : Event
            When set, stop draining immediately.
        """
        BLECompatibilityEventService.drain_publish_queue(
            self,
            flush_event,
            publishing_thread=publishingThread,
        )

    def _publish_connection_status(self, connected: bool) -> None:
        """Enqueue a legacy connection status publish for backward compatibility with tests and integrations.

        Attempts to queue a publish of a "meshtastic.connection.status" message indicating whether this interface is connected; failures to queue or send are handled silently and logged at debug level as a best-effort operation.

        Parameters
        ----------
        connected : bool
            True when the interface is connected, False when disconnected.
        """
        BLECompatibilityEventService.publish_connection_status(
            self,
            connected,
            publishing_thread=publishingThread,
        )

    def _disconnected(self) -> None:
        """Publish the legacy meshtastic.connection.status event indicating the interface is disconnected.

        This enqueues a publish of the connection status for backward compatibility; exceptions raised while queueing or publishing are suppressed.
        """
        super()._disconnected()
        self._publish_connection_status(connected=False)

    def _connected(self) -> None:
        """Mark the interface as connected and publish the legacy connection status event for backwards compatibility."""
        super()._connected()
        self._publish_connection_status(connected=True)
