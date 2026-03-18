"""Management compatibility shim service for BLE."""

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.gating import _is_currently_connected_elsewhere
from meshtastic.interfaces.ble.management_runtime import (
    BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
    BLEManagementCommandHandler,
    _ManagementStartContext,
    T,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface

class BLEManagementCommandsService:
    """Service helpers for BLE management command paths."""

    @staticmethod
    def _handler_for_shim(
        iface: "BLEInterface",
        *,
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
    ) -> BLEManagementCommandHandler:
        """Resolve handler for compatibility shims, preferring iface-owned collaborator."""
        if ble_client_factory is None and connected_elsewhere is None:
            get_handler = getattr(iface, "_get_management_command_handler", None)
            if callable(get_handler):
                resolved = get_handler()
                if isinstance(resolved, BLEManagementCommandHandler):
                    return resolved
        if ble_client_factory is None:
            ble_client_factory = BLEClient
        if connected_elsewhere is None:

            def _default_connected_elsewhere(
                key: str | None, owner: object | None = None
            ) -> bool:
                return _is_currently_connected_elsewhere(key, owner)

            connected_elsewhere = _default_connected_elsewhere
        return BLEManagementCommandHandler(
            iface,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        )

    # COMPAT_STABLE_SHIM: retained for historical monkeypatch/test compatibility.
    @staticmethod
    def _resolve_handler(
        iface: "BLEInterface",
        *,
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
    ) -> BLEManagementCommandHandler:
        """Compatibility alias for shim handler resolution."""
        return BLEManagementCommandsService._handler_for_shim(
            iface,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        )

    # COMPAT_STABLE_SHIM: retain historical helper alias for static compatibility tests.
    @staticmethod
    def _make_handler(
        iface: "BLEInterface",
        *,
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
    ) -> BLEManagementCommandHandler:
        """Compatibility alias for shim handler resolution."""
        return BLEManagementCommandsService._handler_for_shim(
            iface,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        )

    @staticmethod
    def _start_management_phase(
        iface: "BLEInterface", address: str | None
    ) -> _ManagementStartContext:
        """Begin management operation and capture startup context.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing management locks and state helpers.
        address : str | None
            Explicit target address or ``None`` for implicit targeting.

        Returns
        -------
        _ManagementStartContext
            Captured startup context used by downstream management steps.

        Raises
        ------
        BLEError
            Propagated when management preconditions fail.
        Exception
            Re-raises any startup failure after finishing the management
            operation token.
        """
        return BLEManagementCommandsService._handler_for_shim(
            iface
        ).start_management_phase(address)

    @staticmethod
    def _resolve_management_target(
        iface: "BLEInterface",
        address: str | None,
        start_context: _ManagementStartContext,
    ) -> tuple[str | None, BLEClient | None]:
        """Resolve management target address and optional existing client.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing target-resolution and validation helpers.
        address : str | None
            Explicit target address or ``None`` for implicit targeting.
        start_context : _ManagementStartContext
            Context captured at management startup.

        Returns
        -------
        tuple[str | None, BLEClient | None]
            ``(target_address, refreshed_existing_client)`` where the client is
            only populated for existing-client paths that do not require
            temporary-client acquisition.

        Raises
        ------
        BLEError
            If a refreshed existing client disappears or implicit binding
            changes mid-operation.
        """
        return BLEManagementCommandsService._handler_for_shim(
            iface
        ).resolve_management_target(
            address,
            start_context,
        )

    @staticmethod
    def _acquire_client_for_target(
        iface: "BLEInterface",
        *,
        address: str | None,
        target_address: str,
        expected_implicit_binding: str | None,
        connected_elsewhere: Callable[[str | None, object | None], bool],
        ble_client_factory: Callable[..., BLEClient],
    ) -> tuple[BLEClient, BLEClient | None]:
        """Acquire active or temporary client for a resolved management target.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing management/client lookup helpers.
        address : str | None
            Explicit target address or ``None`` for implicit targeting.
        target_address : str
            Resolved management target address.
        expected_implicit_binding : str | None
            Binding snapshot captured at operation startup for revalidation.
        connected_elsewhere : Callable[[str | None, object | None], bool]
            Function used to detect target ownership by another interface.
        ble_client_factory : Callable[..., BLEClient]
            Factory used to create a temporary client when needed.

        Returns
        -------
        tuple[BLEClient, BLEClient | None]
            ``(client_to_use, temporary_client)`` where ``temporary_client`` is
            populated only when a new temporary client was created.

        Raises
        ------
        BLEError
            If target ownership has changed or is currently held elsewhere.
        """
        return BLEManagementCommandsService._handler_for_shim(
            iface,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        ).acquire_client_for_target(
            address=address,
            target_address=target_address,
            expected_implicit_binding=expected_implicit_binding,
        )

    @staticmethod
    def _execute_with_client(
        iface: "BLEInterface",
        *,
        client_to_use: BLEClient,
        temporary_client: BLEClient | None,
        command: Callable[[BLEClient], T],
    ) -> T:
        """Execute a management command and close temporary client on exit.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing client-close helpers.
        client_to_use : BLEClient
            Client passed to ``command``.
        temporary_client : BLEClient | None
            Temporary client to close after execution when present.
        command : Callable[[BLEClient], T]
            Management command callable.

        Returns
        -------
        T
            Value returned by ``command``.

        Raises
        ------
        Exception
            Propagates any exception raised by ``command`` after temporary
            client cleanup is attempted.
        """
        return BLEManagementCommandsService._handler_for_shim(iface).execute_with_client(
            client_to_use=client_to_use,
            temporary_client=temporary_client,
            command=command,
        )

    @staticmethod
    def _execute_management_command(
        iface: "BLEInterface",
        address: str | None,
        command: Callable[[BLEClient], T],
        *,
        ble_client_factory: Callable[..., BLEClient],
        connected_elsewhere: Callable[[str | None, object | None], bool],
    ) -> T:
        """Run management command using active, existing, or temporary BLE client.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing locks, state, and helper methods for management
            operations.
        address : str | None
            Explicit management target address. ``None`` means use current
            implicit management binding.
        command : Callable[[BLEClient], T]
            Management callable executed against the selected client.
        ble_client_factory : Callable[..., BLEClient]
            Factory used to construct a temporary client when no suitable active
            client is available.
        connected_elsewhere : Callable[[str | None, object | None], bool]
            Function used to reject temporary-client creation when a target is
            currently owned by another interface.

        Returns
        -------
        T
            Result returned by ``command``.

        Raises
        ------
        BLEError
            If preconditions fail, target resolution fails, or target ownership
            changes while entering the management gate.
        """
        return BLEManagementCommandsService._handler_for_shim(
            iface,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        ).execute_management_command(
            address,
            command,
        )

    @staticmethod
    def _validate_management_await_timeout(
        iface: "BLEInterface", await_timeout: object
    ) -> float:
        """Validate and return bounded await timeout for management operations.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing the ``BLEError`` type.
        await_timeout : object
            Candidate timeout value.

        Returns
        -------
        float
            Validated positive finite timeout value.

        Raises
        ------
        BLEError
            If ``await_timeout`` is not a finite positive real number.
        """
        return BLEManagementCommandsService._handler_for_shim(
            iface
        ).validate_management_await_timeout(await_timeout)

    @staticmethod
    def _validate_trust_timeout(iface: "BLEInterface", timeout: object) -> float:
        """Validate and return bounded timeout for trust command.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing the ``BLEError`` type.
        timeout : object
            Candidate timeout value.

        Returns
        -------
        float
            Validated positive finite timeout.

        Raises
        ------
        BLEError
            If ``timeout`` is not a finite positive real number.
        """
        return BLEManagementCommandsService._handler_for_shim(
            iface
        ).validate_trust_timeout(timeout)

    @staticmethod
    def _validate_connect_timeout_override(
        iface: "BLEInterface",
        connect_timeout: object,
        *,
        pair_on_connect: bool,
    ) -> None:
        """Validate connect timeout override before orchestration.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing the ``BLEError`` type.
        connect_timeout : object
            Optional override timeout supplied by callers.
        pair_on_connect : bool
            Whether connect attempts request pairing; forwarded to orchestrator
            timeout validation.

        Returns
        -------
        None

        Raises
        ------
        BLEError
            If ``connect_timeout`` is invalid for orchestrated connection logic.
        """
        BLEManagementCommandsService._handler_for_shim(
            iface
        ).validate_connect_timeout_override(
            connect_timeout,
            pair_on_connect=pair_on_connect,
        )

    @staticmethod
    def pair(
        iface: "BLEInterface",
        address: str | None = None,
        *,
        await_timeout: float,
        kwargs: dict[str, object],
        ble_client_factory: Callable[..., BLEClient],
        connected_elsewhere: Callable[[str | None, object | None], bool],
    ) -> None:
        """Pair with BLE device using active or temporary client.

        Parameters
        ----------
        iface : BLEInterface
            Interface used for management orchestration.
        address : str | None
            Optional explicit target address.
        await_timeout : float
            Timeout for BLE pairing call.
        kwargs : dict[str, object]
            Extra keyword arguments forwarded to ``BLEClient.pair``.
        ble_client_factory : Callable[..., BLEClient]
            Factory used for temporary management clients.
        connected_elsewhere : Callable[[str | None, object | None], bool]
            Cross-interface ownership probe used to suppress conflicting
            temporary client creation.

        Returns
        -------
        None
        """
        BLEManagementCommandsService._handler_for_shim(
            iface,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        ).pair(
            address,
            await_timeout=await_timeout,
            kwargs=kwargs,
        )

    @staticmethod
    def unpair(
        iface: "BLEInterface",
        address: str | None = None,
        *,
        await_timeout: float,
        ble_client_factory: Callable[..., BLEClient],
        connected_elsewhere: Callable[[str | None, object | None], bool],
    ) -> None:
        """Unpair BLE device using active or temporary client.

        Parameters
        ----------
        iface : BLEInterface
            Interface used for management orchestration.
        address : str | None
            Optional explicit target address.
        await_timeout : float
            Timeout for BLE unpair call.
        ble_client_factory : Callable[..., BLEClient]
            Factory used for temporary management clients.
        connected_elsewhere : Callable[[str | None, object | None], bool]
            Cross-interface ownership probe used to suppress conflicting
            temporary client creation.

        Returns
        -------
        None
        """
        BLEManagementCommandsService._handler_for_shim(
            iface,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        ).unpair(
            address,
            await_timeout=await_timeout,
        )

    @staticmethod
    def _run_bluetoothctl_trust_command(
        iface: "BLEInterface",
        bluetoothctl_path: str,
        canonical_address: str,
        validated_timeout: float,
        *,
        subprocess_module: object,
        trust_hex_blob_re: re.Pattern[str],
        trust_token_re: re.Pattern[str],
        trust_command_output_max_chars: int,
    ) -> None:
        """Run bluetoothctl trust command and map failures to BLEError.

        Parameters
        ----------
        iface : BLEInterface
            Interface providing ``BLEError`` for mapped failures.
        bluetoothctl_path : str
            Path to ``bluetoothctl`` executable.
        canonical_address : str
            Address passed to ``bluetoothctl trust``.
        validated_timeout : float
            Positive finite timeout for subprocess execution.
        subprocess_module : object
            Injected subprocess-like module for runtime/test compatibility.
        trust_hex_blob_re : re.Pattern[str]
            Pattern used to redact long hexadecimal blobs from command output.
        trust_token_re : re.Pattern[str]
            Pattern used to redact long token-like output fragments.
        trust_command_output_max_chars : int
            Maximum sanitized output characters included in raised errors.

        Returns
        -------
        None

        Raises
        ------
        BLEError
            If command execution fails, times out, or returns non-zero status.
        """
        BLEManagementCommandsService._handler_for_shim(
            iface
        ).run_bluetoothctl_trust_command(
            bluetoothctl_path,
            canonical_address,
            validated_timeout,
            subprocess_module=subprocess_module,
            trust_hex_blob_re=trust_hex_blob_re,
            trust_token_re=trust_token_re,
            trust_command_output_max_chars=trust_command_output_max_chars,
        )

    @staticmethod
    def trust(
        iface: "BLEInterface",
        address: str | None = None,
        *,
        timeout: float = BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
        sys_module: object,
        shutil_module: object,
        subprocess_module: object,
        trust_hex_blob_re: re.Pattern[str],
        trust_token_re: re.Pattern[str],
        trust_command_output_max_chars: int,
    ) -> None:
        """Mark BLE device as trusted via Linux bluetoothctl.

        Parameters
        ----------
        iface : BLEInterface
            Interface used for management/target-gating orchestration.
        address : str | None
            Optional explicit target address. ``None`` uses implicit binding.
        timeout : float
            Command timeout in seconds.
        sys_module : object
            Injected ``sys``-compatible module.
        shutil_module : object
            Injected ``shutil``-compatible module.
        subprocess_module : object
            Injected ``subprocess``-compatible module.
        trust_hex_blob_re : re.Pattern[str]
            Redaction pattern for long hexadecimal output fragments.
        trust_token_re : re.Pattern[str]
            Redaction pattern for long token-like output fragments.
        trust_command_output_max_chars : int
            Maximum sanitized output characters included in raised errors.

        Returns
        -------
        None

        Raises
        ------
        BLEError
            If address validation, environment preconditions, target resolution,
            or command execution fails.
        """
        BLEManagementCommandsService._handler_for_shim(iface).trust(
            address,
            timeout=timeout,
            sys_module=sys_module,
            shutil_module=shutil_module,
            subprocess_module=subprocess_module,
            trust_hex_blob_re=trust_hex_blob_re,
            trust_token_re=trust_token_re,
            trust_command_output_max_chars=trust_command_output_max_chars,
        )
