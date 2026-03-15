"""Management command helpers for BLE interface orchestration."""

import inspect
import math
import numbers
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from meshtastic.interfaces.ble.client import BLEClient
from meshtastic.interfaces.ble.connection import ConnectionOrchestrator
from meshtastic.interfaces.ble.constants import (
    ERROR_CONNECTION_SUPPRESSED,
    ERROR_INVALID_CONNECT_TIMEOUT,
    ERROR_MANAGEMENT_ADDRESS_EMPTY,
    ERROR_MANAGEMENT_ADDRESS_REQUIRED,
    ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID,
    ERROR_MANAGEMENT_TARGET_CHANGED,
    ERROR_TRUST_BLUETOOTHCTL_MISSING,
    ERROR_TRUST_COMMAND_FAILED,
    ERROR_TRUST_COMMAND_TIMEOUT,
    ERROR_TRUST_INVALID_TIMEOUT,
    ERROR_TRUST_LINUX_ONLY,
    logger,
)
from meshtastic.interfaces.ble.gating import (
    _addr_key,
)
from meshtastic.interfaces.ble.utils import sanitize_address

if TYPE_CHECKING:
    from meshtastic.interfaces.ble.interface import BLEInterface


BLUETOOTHCTL_TRUST_TIMEOUT_SECONDS: float = 10.0
TRUST_COMMAND_OUTPUT_MAX_CHARS: int = 200
TRUST_HEX_BLOB_RE = re.compile(r"\b[0-9A-Fa-f]{16,}\b")
TRUST_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")
_DISCOVERY_FACTORY_LOG_KWARG = "log_if_no_address"
_UNEXPECTED_KEYWORD_FRAGMENT = "unexpected keyword argument"

T = TypeVar("T")


@dataclass(frozen=True)
class _ManagementStartContext:
    """Captured state from management-operation startup validation."""

    expected_implicit_binding: str | None
    target_address: str | None
    use_existing_client_without_resolved_address: bool


def _is_unexpected_keyword_error(exc: TypeError, kwarg_name: str) -> bool:
    """Return True when a TypeError clearly indicates an unsupported keyword arg."""
    message = str(exc)
    return _UNEXPECTED_KEYWORD_FRAGMENT in message and f"'{kwarg_name}'" in message


def _create_management_client(
    ble_client_factory: Callable[..., BLEClient], target_address: str
) -> BLEClient:
    """Create a temporary BLE client while tolerating factories without kwargs."""
    try:
        signature = inspect.signature(ble_client_factory)
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
        if not accepts_log_kwarg:
            return ble_client_factory(target_address)

    try:
        return ble_client_factory(target_address, log_if_no_address=False)
    except TypeError as exc:
        if _is_unexpected_keyword_error(exc, _DISCOVERY_FACTORY_LOG_KWARG):
            logger.debug(
                "Management client factory rejected log_if_no_address kwarg; retrying without it: %s",
                exc,
                exc_info=True,
            )
            return ble_client_factory(target_address)
        raise


class BLEManagementCommandsService:
    """Service helpers for BLE management command paths."""

    @staticmethod
    def _start_management_phase(
        iface: "BLEInterface", address: str | None
    ) -> _ManagementStartContext:
        """Begin management operation and capture startup context."""
        with iface._connect_lock, iface._management_lock:
            iface._validate_management_preconditions()
            iface._begin_management_operation_locked()
            try:
                expected_implicit_binding = None
                if address is None:
                    with iface._state_lock:
                        expected_implicit_binding = (
                            iface._get_current_implicit_management_binding_locked()
                        )
                existing_client = iface._get_management_client_if_available(address)
                target_address = iface._extract_client_address(existing_client)
                use_existing_client_without_resolved_address = (
                    existing_client is not None and target_address is None
                )
            except BaseException:
                iface._finish_management_operation()
                raise
        return _ManagementStartContext(
            expected_implicit_binding=expected_implicit_binding,
            target_address=target_address,
            use_existing_client_without_resolved_address=use_existing_client_without_resolved_address,
        )

    @staticmethod
    def _resolve_management_target(
        iface: "BLEInterface",
        address: str | None,
        start_context: _ManagementStartContext,
    ) -> tuple[str | None, BLEClient | None]:
        """Resolve target address or refresh an existing-client-only path."""
        target_address = start_context.target_address
        if (
            target_address is None
            and not start_context.use_existing_client_without_resolved_address
        ):
            target_address = iface._resolve_target_address_for_management(address)

        if start_context.use_existing_client_without_resolved_address:
            with iface._connect_lock, iface._management_lock:
                iface._validate_management_preconditions()
                refreshed_existing_client = iface._get_management_client_if_available(
                    address
                )
                if refreshed_existing_client is None:
                    raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
                if address is None:
                    with iface._state_lock:
                        current_binding = (
                            iface._get_current_implicit_management_binding_locked()
                        )
                    if sanitize_address(current_binding) != sanitize_address(
                        start_context.expected_implicit_binding
                    ):
                        raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
            return target_address, refreshed_existing_client

        return target_address, None

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
        """Acquire active or temporary client for a resolved management target."""
        client_to_use: BLEClient | None = None
        target_key: str | None = None
        temporary_client: BLEClient | None = None

        with iface._connect_lock, iface._management_lock:
            iface._validate_management_preconditions()
            if address is None:
                iface._revalidate_implicit_management_target(
                    target_address,
                    expected_binding=expected_implicit_binding,
                )
            client_to_use = iface._get_management_client_for_target(
                target_address,
                prefer_current_client=address is None,
            )
            if client_to_use is None:
                target_key = _addr_key(target_address)

        if client_to_use is None:
            if target_key is not None and connected_elsewhere(target_key, iface):
                raise iface.BLEError(ERROR_CONNECTION_SUPPRESSED)
            temporary_client = _create_management_client(ble_client_factory, target_address)
            client_to_use = temporary_client

        return client_to_use, temporary_client

    @staticmethod
    def _execute_with_client(
        iface: "BLEInterface",
        *,
        client_to_use: BLEClient,
        temporary_client: BLEClient | None,
        command: Callable[[BLEClient], T],
    ) -> T:
        """Execute management command and close temporary client on exit."""
        try:
            return command(client_to_use)
        finally:
            if temporary_client is not None:
                iface._client_manager_safe_close_client(temporary_client)

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
        if address is not None and sanitize_address(address) is None:
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)

        management_started = False
        try:
            start_context = BLEManagementCommandsService._start_management_phase(
                iface, address
            )
            management_started = True
            target_address, refreshed_existing_client = (
                BLEManagementCommandsService._resolve_management_target(
                    iface,
                    address,
                    start_context,
                )
            )

            if refreshed_existing_client is not None:
                return command(refreshed_existing_client)

            if target_address is None:
                raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_REQUIRED)

            with iface._management_target_gate(target_address):
                client_to_use, temporary_client = (
                    BLEManagementCommandsService._acquire_client_for_target(
                        iface,
                        address=address,
                        target_address=target_address,
                        expected_implicit_binding=start_context.expected_implicit_binding,
                        connected_elsewhere=connected_elsewhere,
                        ble_client_factory=ble_client_factory,
                    )
                )
                return BLEManagementCommandsService._execute_with_client(
                    iface,
                    client_to_use=client_to_use,
                    temporary_client=temporary_client,
                    command=command,
                )
        finally:
            if management_started:
                iface._finish_management_operation()

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
        if (
            await_timeout is None
            or isinstance(await_timeout, bool)
            or not isinstance(await_timeout, numbers.Real)
            or not math.isfinite(await_timeout)
            or await_timeout <= 0
        ):
            raise iface.BLEError(ERROR_MANAGEMENT_AWAIT_TIMEOUT_INVALID)
        return float(await_timeout)

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
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, numbers.Real)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise iface.BLEError(ERROR_TRUST_INVALID_TIMEOUT)
        return float(timeout)

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
        if connect_timeout is None:
            return
        if isinstance(connect_timeout, bool) or not isinstance(
            connect_timeout, numbers.Real
        ):
            exc = ValueError(
                "connect_timeout must be a finite positive number of seconds."
            )
            raise iface.BLEError(ERROR_INVALID_CONNECT_TIMEOUT.format(exc=exc)) from exc
        try:
            ConnectionOrchestrator._resolve_connect_timeout(
                pair_on_connect=pair_on_connect,
                connect_timeout=float(connect_timeout),
            )
        except (TypeError, ValueError) as exc:
            raise iface.BLEError(ERROR_INVALID_CONNECT_TIMEOUT.format(exc=exc)) from exc

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
        validated_timeout = (
            BLEManagementCommandsService._validate_management_await_timeout(
            iface, await_timeout
            )
        )
        BLEManagementCommandsService._execute_management_command(
            iface,
            address,
            lambda client: client.pair(await_timeout=validated_timeout, **kwargs),
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
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
        validated_timeout = (
            BLEManagementCommandsService._validate_management_await_timeout(
            iface, await_timeout
            )
        )
        BLEManagementCommandsService._execute_management_command(
            iface,
            address,
            lambda client: client.unpair(await_timeout=validated_timeout),
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
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

        def _sanitize_trust_command_output(output: str) -> str:
            sanitized = " ".join(output.splitlines())
            sanitized = "".join(ch if ch.isprintable() else "?" for ch in sanitized)
            sanitized = trust_hex_blob_re.sub("[redacted-hex]", sanitized)
            sanitized = trust_token_re.sub("[redacted-token]", sanitized)
            sanitized = " ".join(sanitized.split())
            max_output_chars = max(trust_command_output_max_chars, 0)
            if len(sanitized) > max_output_chars:
                if max_output_chars <= 3:
                    return sanitized[:max_output_chars]
                max_prefix = max_output_chars - 3
                return f"{sanitized[:max_prefix]}..."
            return sanitized

        logger.debug(
            "Running bluetoothctl trust command: %s (timeout=%.1fs)",
            [bluetoothctl_path, "trust", canonical_address],
            validated_timeout,
        )
        try:
            result = subprocess_module.run(  # type: ignore[attr-defined]  # noqa: S603
                [bluetoothctl_path, "trust", canonical_address],
                capture_output=True,
                text=True,
                check=False,
                timeout=validated_timeout,
            )
        except subprocess_module.TimeoutExpired as exc:  # type: ignore[attr-defined]
            raise iface.BLEError(
                ERROR_TRUST_COMMAND_TIMEOUT.format(
                    timeout=validated_timeout, address=canonical_address
                )
            ) from exc
        except OSError as exc:
            detail = _sanitize_trust_command_output(str(exc))
            raise iface.BLEError(
                ERROR_TRUST_COMMAND_FAILED.format(
                    address=canonical_address,
                    detail=(
                        f"{bluetoothctl_path}: {detail}"
                        if detail
                        else f"{bluetoothctl_path}: unable to execute bluetoothctl"
                    ),
                )
            ) from exc
        if result.returncode != 0:
            stderr_output = (
                _sanitize_trust_command_output(result.stderr) if result.stderr else ""
            )
            stdout_output = (
                _sanitize_trust_command_output(result.stdout) if result.stdout else ""
            )
            detail_parts = []
            if stderr_output:
                detail_parts.append(f"stderr: {stderr_output}")
            if stdout_output:
                detail_parts.append(f"stdout: {stdout_output}")
            detail = " | ".join(detail_parts) or f"exit code {result.returncode}"
            raise iface.BLEError(
                ERROR_TRUST_COMMAND_FAILED.format(
                    address=canonical_address, detail=detail
                )
            )
        logger.info("Trusted BLE device via bluetoothctl: %s", canonical_address)

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
        if address is not None and sanitize_address(address) is None:
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)

        expected_implicit_binding = None

        validated_timeout = BLEManagementCommandsService._validate_trust_timeout(
            iface, timeout
        )
        if not sys_module.platform.startswith("linux"):  # type: ignore[attr-defined]
            raise iface.BLEError(ERROR_TRUST_LINUX_ONLY)
        bluetoothctl_path = shutil_module.which("bluetoothctl")  # type: ignore[attr-defined]
        if bluetoothctl_path is None:
            raise iface.BLEError(ERROR_TRUST_BLUETOOTHCTL_MISSING)

        management_started = False
        try:
            with iface._connect_lock, iface._management_lock:
                iface._validate_management_preconditions()
                iface._begin_management_operation_locked()
                management_started = True
                if address is None:
                    with iface._state_lock:
                        expected_implicit_binding = (
                            iface._get_current_implicit_management_binding_locked()
                        )
            target_address = iface._resolve_target_address_for_management(address)
            canonical_address = iface._format_bluetoothctl_address(target_address)
            with iface._management_target_gate(target_address):
                if address is None:
                    with iface._connect_lock, iface._management_lock:
                        iface._validate_management_preconditions()
                        iface._revalidate_implicit_management_target(
                            target_address,
                            expected_binding=expected_implicit_binding,
                        )
                else:
                    with iface._connect_lock, iface._management_lock:
                        iface._validate_management_preconditions()
                BLEManagementCommandsService._run_bluetoothctl_trust_command(
                    iface,
                    bluetoothctl_path,
                    canonical_address,
                    validated_timeout,
                    subprocess_module=subprocess_module,
                    trust_hex_blob_re=trust_hex_blob_re,
                    trust_token_re=trust_token_re,
                    trust_command_output_max_chars=trust_command_output_max_chars,
                )
        finally:
            if management_started:
                iface._finish_management_operation()
