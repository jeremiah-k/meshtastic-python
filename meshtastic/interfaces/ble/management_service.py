"""Management command helpers for BLE interface orchestration."""

import math
import numbers
import re
from collections.abc import Callable
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

T = TypeVar("T")


class BLEManagementCommandsService:
    """Service helpers for BLE management command paths."""

    @staticmethod
    def execute_management_command(
        iface: "BLEInterface",
        address: str | None,
        command: Callable[[BLEClient], T],
        *,
        ble_client_factory: Callable[..., BLEClient],
        connected_elsewhere: Callable[[str | None, object | None], bool],
    ) -> T:
        """Run management command using active, existing, or temporary BLE client."""
        if address is not None and sanitize_address(address) is None:
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)

        management_started = False
        try:
            with iface._connect_lock, iface._management_lock:
                iface._validate_management_preconditions()
                iface._begin_management_operation_locked()
                management_started = True
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
            if (
                target_address is None
                and not use_existing_client_without_resolved_address
            ):
                target_address = iface._resolve_target_address_for_management(address)

            if use_existing_client_without_resolved_address:
                with iface._connect_lock:
                    with iface._management_lock:
                        iface._validate_management_preconditions()
                        refreshed_existing_client = (
                            iface._get_management_client_if_available(address)
                        )
                        if refreshed_existing_client is None:
                            raise iface.BLEError(ERROR_MANAGEMENT_TARGET_CHANGED)
                return command(refreshed_existing_client)

            if target_address is None:
                raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_REQUIRED)
            with iface._management_target_gate(target_address):
                with iface._connect_lock:
                    with iface._management_lock:
                        iface._validate_management_preconditions()
                        if address is None:
                            iface._revalidate_implicit_management_target(
                                target_address,
                                expected_binding=expected_implicit_binding,
                            )
                        existing_client = iface._get_management_client_for_target(
                            target_address,
                            prefer_current_client=address is None,
                        )
                        if existing_client is not None:
                            client_to_use = existing_client
                            temporary_client = None
                        else:
                            target_key = _addr_key(target_address)
                            if (
                                target_key is not None
                                and connected_elsewhere(target_key, iface)
                            ):
                                raise iface.BLEError(ERROR_CONNECTION_SUPPRESSED)
                            temporary_client = ble_client_factory(
                                target_address, log_if_no_address=False
                            )
                            client_to_use = temporary_client

                    try:
                        return command(client_to_use)
                    finally:
                        if temporary_client is not None:
                            iface._client_manager_safe_close_client(temporary_client)
        finally:
            if management_started:
                iface._finish_management_operation()

    @staticmethod
    def validate_management_await_timeout(
        iface: "BLEInterface", await_timeout: object
    ) -> float:
        """Validate and return bounded await timeout for management operations."""
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
    def validate_trust_timeout(iface: "BLEInterface", timeout: object) -> float:
        """Validate and return bounded timeout for trust command."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, numbers.Real)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise iface.BLEError(ERROR_TRUST_INVALID_TIMEOUT)
        return float(timeout)

    @staticmethod
    def validate_connect_timeout_override(
        iface: "BLEInterface",
        connect_timeout: object,
        *,
        pair_on_connect: bool,
    ) -> None:
        """Validate connect timeout override before orchestration."""
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
        """Pair with BLE device using active or temporary client."""
        validated_timeout = BLEManagementCommandsService.validate_management_await_timeout(
            iface, await_timeout
        )
        BLEManagementCommandsService.execute_management_command(
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
        """Unpair BLE device using active or temporary client."""
        validated_timeout = BLEManagementCommandsService.validate_management_await_timeout(
            iface, await_timeout
        )
        BLEManagementCommandsService.execute_management_command(
            iface,
            address,
            lambda client: client.unpair(await_timeout=validated_timeout),
            ble_client_factory=ble_client_factory,
            connected_elsewhere=connected_elsewhere,
        )

    @staticmethod
    def run_bluetoothctl_trust_command(
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
        """Run bluetoothctl trust command and map failures to BLEError."""

        def _sanitize_trust_command_output(output: str) -> str:
            sanitized = " ".join(output.splitlines())
            sanitized = "".join(ch if ch.isprintable() else "?" for ch in sanitized)
            sanitized = trust_hex_blob_re.sub("[redacted-hex]", sanitized)
            sanitized = trust_token_re.sub("[redacted-token]", sanitized)
            sanitized = " ".join(sanitized.split())
            if len(sanitized) > trust_command_output_max_chars:
                max_prefix = trust_command_output_max_chars - 3
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
        """Mark BLE device as trusted via Linux bluetoothctl."""
        if address is not None and sanitize_address(address) is None:
            raise iface.BLEError(ERROR_MANAGEMENT_ADDRESS_EMPTY)

        with iface._connect_lock, iface._management_lock:
            iface._validate_management_preconditions()
            expected_implicit_binding = None
            if address is None:
                with iface._state_lock:
                    expected_implicit_binding = (
                        iface._get_current_implicit_management_binding_locked()
                    )

        validated_timeout = BLEManagementCommandsService.validate_trust_timeout(
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
            target_address = iface._resolve_target_address_for_management(address)
            canonical_address = iface._format_bluetoothctl_address(target_address)
            with iface._management_target_gate(target_address):
                if address is None:
                    with iface._connect_lock:
                        with iface._management_lock:
                            iface._validate_management_preconditions()
                            iface._revalidate_implicit_management_target(
                                target_address,
                                expected_binding=expected_implicit_binding,
                            )
                        BLEManagementCommandsService.run_bluetoothctl_trust_command(
                            iface,
                            bluetoothctl_path,
                            canonical_address,
                            validated_timeout,
                            subprocess_module=subprocess_module,
                            trust_hex_blob_re=trust_hex_blob_re,
                            trust_token_re=trust_token_re,
                            trust_command_output_max_chars=trust_command_output_max_chars,
                        )
                else:
                    with iface._connect_lock, iface._management_lock:
                        iface._validate_management_preconditions()
                    BLEManagementCommandsService.run_bluetoothctl_trust_command(
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
