"""Management compatibility shim service for BLE."""

import re
import shutil as shutil_stdlib
import subprocess as subprocess_stdlib
import sys as sys_stdlib
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.constants import logger
from meshtastic.interfaces.ble.gating import _is_currently_connected_elsewhere
from meshtastic.interfaces.ble.management_runtime import (
    BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS,
    TRUST_COMMAND_OUTPUT_MAX_CHARS,
    TRUST_HEX_BLOB_RE,
    TRUST_TOKEN_RE,
    BLEManagementCommandHandler,
    T,
    _ManagementStartContext,
)
from meshtastic.interfaces.ble.utils import (
    _is_unconfigured_mock_callable,
    _is_unconfigured_mock_member,
)

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


class BLEManagementCommandsService:
    """Service helpers for BLE management command paths."""

    # COMPAT_STABLE_SHIM: compatibility management shim surface retained for historical entrypoints.

    @staticmethod
    def _has_required_handler_entrypoint(
        candidate: object, expected_method: str | None = None
    ) -> bool:
        """Return whether ``candidate`` exposes usable shim entrypoint(s).

        When ``expected_method`` is provided, this requires that specific
        delegated method to be callable/non-mock. Otherwise, it returns ``True``
        as soon as it finds any callable/non-mock method from the compatibility
        entrypoint list.
        """
        if _is_unconfigured_mock_member(candidate):
            return False
        if expected_method is not None:
            expected = getattr(candidate, expected_method, None)
            return callable(expected) and not _is_unconfigured_mock_callable(expected)
        required_entrypoints = (
            "execute_management_command",
            "start_management_phase",
            "resolve_management_target",
            "acquire_client_for_target",
            "execute_with_client",
            "pair",
            "unpair",
            "trust",
            "validate_management_await_timeout",
            "validate_trust_timeout",
            "validate_connect_timeout_override",
            "run_bluetoothctl_trust_command",
        )
        for method_name in required_entrypoints:
            method = getattr(candidate, method_name, None)
            if callable(method) and not _is_unconfigured_mock_callable(method):
                return True
        return False

    @staticmethod
    def _is_handler_like(candidate: object) -> bool:
        """Return whether ``candidate`` exposes the management handler API surface."""
        if _is_unconfigured_mock_member(candidate):
            return False
        required_methods = (
            "resolve_target_address_for_management",
            "management_target_gate",
            "get_management_client_if_available",
            "get_management_client_for_target",
            "get_current_implicit_management_binding_locked",
            "get_current_implicit_management_address_locked",
            "revalidate_implicit_management_target",
            "start_management_phase",
            "resolve_management_target",
            "acquire_client_for_target",
            "execute_with_client",
            "execute_management_command",
            "begin_management_operation_locked",
            "finish_management_operation",
            "validate_management_await_timeout",
            "validate_trust_timeout",
            "validate_connect_timeout_override",
            "pair",
            "unpair",
            "run_bluetoothctl_trust_command",
            "trust",
        )
        for method_name in required_methods:
            method = getattr(candidate, method_name, None)
            if not callable(method) or _is_unconfigured_mock_callable(method):
                return False
        return True

    @staticmethod
    def _handler_for_shim(
        iface: "BLEInterface",
        *,
        expected_method: str | None = None,
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
    ) -> BLEManagementCommandHandler:
        """Resolve handler for compatibility shims, preferring iface-owned collaborator."""
        use_iface_owned_handler = (
            ble_client_factory is None and connected_elsewhere is None
        )
        if use_iface_owned_handler:
            get_handler: object | None = None
            try:
                get_handler = getattr(iface, "_get_management_command_handler", None)
            except AttributeError:
                logger.debug(
                    "Error resolving _get_management_command_handler; falling back to direct handler field.",
                    exc_info=True,
                )
            if callable(get_handler) and not _is_unconfigured_mock_callable(
                get_handler
            ):
                try:
                    resolved = get_handler()
                except (AttributeError, TypeError):
                    logger.debug(
                        "Error calling _get_management_command_handler; falling back to direct handler field.",
                        exc_info=True,
                    )
                else:
                    if (
                        resolved is not None
                        and not _is_unconfigured_mock_member(resolved)
                        and (
                            BLEManagementCommandsService._is_handler_like(resolved)
                            # COMPAT_STABLE_SHIM: preserve iface-owned partial
                            # handler/proxy doubles used by historical shim paths.
                            or BLEManagementCommandsService._has_required_handler_entrypoint(
                                resolved,
                                expected_method,
                            )
                        )
                    ):
                        return cast(BLEManagementCommandHandler, resolved)
            direct_handler = getattr(iface, "_management_command_handler", None)
            if (
                direct_handler is not None
                and not _is_unconfigured_mock_member(direct_handler)
                and (
                    BLEManagementCommandsService._is_handler_like(direct_handler)
                    # COMPAT_STABLE_SHIM: preserve iface-owned partial
                    # handler/proxy doubles used by historical shim paths.
                    or BLEManagementCommandsService._has_required_handler_entrypoint(
                        direct_handler,
                        expected_method,
                    )
                )
            ):
                return cast(BLEManagementCommandHandler, direct_handler)
        if ble_client_factory is None:
            ble_client_factory = BLEClient
        if connected_elsewhere is None:
            iface_connected_elsewhere = getattr(
                iface, "_connected_elsewhere_late_bound", None
            )
            if callable(
                iface_connected_elsewhere
            ) and not _is_unconfigured_mock_callable(iface_connected_elsewhere):
                connected_elsewhere = cast(
                    Callable[[str | None, object | None], bool],
                    iface_connected_elsewhere,
                )
            else:

                def _default_connected_elsewhere(
                    key: str | None, owner: object | None = None
                ) -> bool:
                    return _is_currently_connected_elsewhere(key, owner=owner)

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
        expected_method: str | None = None,
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
    ) -> BLEManagementCommandHandler:
        """Compatibility alias for shim handler resolution."""
        return BLEManagementCommandsService._handler_for_shim(
            iface,
            expected_method=expected_method,
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        )

    # COMPAT_STABLE_SHIM: retain historical helper alias for static compatibility tests.
    @staticmethod
    def _make_handler(
        iface: "BLEInterface",
        *,
        expected_method: str | None = None,
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
    ) -> BLEManagementCommandHandler:
        """Compatibility alias for shim handler resolution."""
        return BLEManagementCommandsService._handler_for_shim(
            iface,
            expected_method=expected_method,
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
            iface,
            expected_method="start_management_phase",
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
            iface,
            expected_method="resolve_management_target",
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
            expected_method="acquire_client_for_target",
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
        return BLEManagementCommandsService._handler_for_shim(
            iface,
            expected_method="execute_with_client",
        ).execute_with_client(
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
            expected_method="execute_management_command",
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
            iface,
            expected_method="validate_management_await_timeout",
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
            iface,
            expected_method="validate_trust_timeout",
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
            iface,
            expected_method="validate_connect_timeout_override",
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
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
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
        ble_client_factory : Callable[..., BLEClient] | None
            Optional factory used for temporary management clients.
            ``None`` uses default shim resolution.
        connected_elsewhere : Callable[[str | None, object | None], bool] | None
            Cross-interface ownership probe used to suppress conflicting
            temporary client creation. ``None`` uses default shim resolution.

        Returns
        -------
        None
        """
        BLEManagementCommandsService._handler_for_shim(
            iface,
            expected_method="pair",
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
        ble_client_factory: Callable[..., BLEClient] | None = None,
        connected_elsewhere: Callable[[str | None, object | None], bool] | None = None,
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
        ble_client_factory : Callable[..., BLEClient] | None
            Optional factory used for temporary management clients.
            ``None`` uses default shim resolution.
        connected_elsewhere : Callable[[str | None, object | None], bool] | None
            Cross-interface ownership probe used to suppress conflicting
            temporary client creation. ``None`` uses default shim resolution.

        Returns
        -------
        None
        """
        BLEManagementCommandsService._handler_for_shim(
            iface,
            expected_method="unpair",
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
            iface,
            expected_method="run_bluetoothctl_trust_command",
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
        sys_module: object | None = None,
        shutil_module: object | None = None,
        subprocess_module: object | None = None,
        trust_hex_blob_re: re.Pattern[str] | None = None,
        trust_token_re: re.Pattern[str] | None = None,
        trust_command_output_max_chars: int | None = None,
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
        sys_module : object | None
            Optional injected ``sys``-compatible module. ``None`` uses stdlib ``sys``.
        shutil_module : object | None
            Optional injected ``shutil``-compatible module. ``None`` uses stdlib ``shutil``.
        subprocess_module : object | None
            Optional injected ``subprocess``-compatible module. ``None`` uses stdlib ``subprocess``.
        trust_hex_blob_re : re.Pattern[str] | None
            Optional redaction pattern for long hexadecimal output fragments.
            ``None`` uses runtime default.
        trust_token_re : re.Pattern[str] | None
            Optional redaction pattern for long token-like output fragments.
            ``None`` uses runtime default.
        trust_command_output_max_chars : int | None
            Optional maximum sanitized output length. ``None`` uses runtime default.

        Returns
        -------
        None

        Raises
        ------
        BLEError
            If address validation, environment preconditions, target resolution,
            or command execution fails.
        """
        sys_module = sys_stdlib if sys_module is None else sys_module
        shutil_module = shutil_stdlib if shutil_module is None else shutil_module
        subprocess_module = (
            subprocess_stdlib if subprocess_module is None else subprocess_module
        )
        trust_hex_blob_re = (
            TRUST_HEX_BLOB_RE if trust_hex_blob_re is None else trust_hex_blob_re
        )
        trust_token_re = TRUST_TOKEN_RE if trust_token_re is None else trust_token_re
        trust_command_output_max_chars = (
            TRUST_COMMAND_OUTPUT_MAX_CHARS
            if trust_command_output_max_chars is None
            else trust_command_output_max_chars
        )
        BLEManagementCommandsService._handler_for_shim(
            iface,
            expected_method="trust",
        ).trust(
            address,
            timeout=timeout,
            sys_module=sys_module,
            shutil_module=shutil_module,
            subprocess_module=subprocess_module,
            trust_hex_blob_re=trust_hex_blob_re,
            trust_token_re=trust_token_re,
            trust_command_output_max_chars=trust_command_output_max_chars,
        )
