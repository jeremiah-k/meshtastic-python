"""Utility functions for BLE operations."""

import asyncio
import importlib
import inspect
import time
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any, TypeVar, cast
from unittest.mock import DEFAULT, Mock, NonCallableMock

from meshtastic.interfaces.ble.constants import logger

T = TypeVar("T")
_UNEXPECTED_KEYWORD_FRAGMENT = "unexpected keyword argument"
_POSITIONAL_ONLY_KEYWORD_FRAGMENT = (
    "positional-only arguments passed as keyword arguments"
)


def _is_unconfigured_mock_member(candidate: object) -> bool:
    """Return whether a candidate is an unconfigured auto-generated mock member.

    Parameters
    ----------
    candidate : object
        Candidate object to inspect.

    Returns
    -------
    bool
        True when the object is a mock member with default return behavior,
        no side effect, and no recorded calls.
    """
    if not isinstance(candidate, (Mock, NonCallableMock)):
        return False
    return (
        getattr(candidate, "_mock_return_value", DEFAULT) is DEFAULT
        and getattr(candidate, "_mock_wraps", None) is None
        and getattr(candidate, "side_effect", None) is None
        and not getattr(candidate, "call_args_list", [])
    )


def _is_unconfigured_mock_callable(candidate: object) -> bool:
    """Return whether a candidate is an unconfigured callable auto-generated mock.

    Parameters
    ----------
    candidate : object
        Candidate object to inspect.

    Returns
    -------
    bool
        True when candidate is callable and also matches
        ``_is_unconfigured_mock_member``.
    """
    return callable(candidate) and _is_unconfigured_mock_member(candidate)


def _is_unexpected_keyword_error(exc: TypeError, kwarg_name: str) -> bool:
    """Return whether a TypeError indicates an unsupported keyword argument.

    Parameters
    ----------
    exc : TypeError
        Exception raised by a callable invocation.
    kwarg_name : str
        Keyword argument name that may be unsupported by the callable.

    Returns
    -------
    bool
        ``True`` when ``exc`` clearly reports an unexpected ``kwarg_name``
        keyword argument.
    """
    message = str(exc)
    quoted_kwarg = f"'{kwarg_name}'"
    has_keyword_mismatch = _UNEXPECTED_KEYWORD_FRAGMENT in message
    has_positional_only_mismatch = (
        _POSITIONAL_ONLY_KEYWORD_FRAGMENT in message or "positional-only" in message
    )
    return quoted_kwarg in message and (
        has_keyword_mismatch or has_positional_only_mismatch
    )


def _call_factory_with_optional_kwarg(
    factory: Callable[..., T],
    *,
    args: tuple[Any, ...] = (),
    optional_kwarg: str,
    optional_value: object,
    on_kwarg_rejected: Callable[[TypeError], None] | None = None,
) -> T:
    """Call a factory while tolerating missing support for one optional kwarg.

    Parameters
    ----------
    factory : Callable[..., T]
        Factory callable to invoke.
    args : tuple[Any, ...]
        Positional arguments passed to ``factory``.
    optional_kwarg : str
        Optional keyword argument name to pass when supported.
    optional_value : object
        Value used for ``optional_kwarg`` when passed.
    on_kwarg_rejected : Callable[[TypeError], None] | None
        Optional callback invoked when the optional keyword is rejected by the
        factory and the helper retries without it.

    Returns
    -------
    T
        Factory return value.

    Raises
    ------
    TypeError
        Propagated when the factory raises ``TypeError`` for reasons other than
        rejecting ``optional_kwarg``.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        signature = None

    accepts_optional_kwarg = False
    if signature is not None:
        optional_parameter = signature.parameters.get(optional_kwarg)
        accepts_optional_kwarg = (
            optional_parameter is not None
            and optional_parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        ) or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if not accepts_optional_kwarg:
            return factory(*args)

    try:
        return factory(*args, **{optional_kwarg: optional_value})
    except TypeError as exc:
        if _is_unexpected_keyword_error(exc, optional_kwarg):
            if on_kwarg_rejected is not None:
                on_kwarg_rejected(exc)
            return factory(*args)
        raise


def _resolve_safe_execute(iface: object) -> Callable[..., Any] | None:
    """Resolve an error-handler safe_execute hook with underscore fallback.

    Parameters
    ----------
    iface : object
        Interface-like object exposing an ``error_handler`` member.

    Returns
    -------
    Callable[..., Any] | None
        Resolved ``safe_execute`` callable when available and configured;
        otherwise ``None``.
    """
    error_handler = getattr(iface, "error_handler", None)
    safe_execute = getattr(error_handler, "safe_execute", None)
    if callable(safe_execute) and not _is_unconfigured_mock_callable(safe_execute):
        return cast(Callable[..., Any], safe_execute)
    legacy_safe_execute = getattr(error_handler, "_safe_execute", None)
    if callable(legacy_safe_execute) and not _is_unconfigured_mock_callable(
        legacy_safe_execute
    ):
        return cast(Callable[..., Any], legacy_safe_execute)
    return None


def _safe_execute_through_adapter(
    iface: object,
    func: Callable[[], T],
    *,
    default_return: T | None = None,
    error_msg: str,
    reraise: bool = False,
) -> T | None:
    """Execute a callable through compatibility-safe ``safe_execute`` hooks.

    Parameters
    ----------
    iface : object
        Interface-like object exposing an ``error_handler`` member.
    func : Callable[[], T]
        Zero-argument callable to execute.
    default_return : T | None
        Value returned when execution fails and ``reraise`` is ``False``.
    error_msg : str
        Log message used when execution fails.
    reraise : bool
        When ``True``, re-raise execution exceptions after logging.

    Returns
    -------
    T | None
        Callable return value on success; otherwise ``default_return``.

    Raises
    ------
    Exception
        Re-raises failures only when ``reraise`` is ``True``.
    """
    safe_execute = _resolve_safe_execute(iface)
    if safe_execute is not None:
        adapter_kwargs: dict[str, object] = {
            "default_return": default_return,
            "error_msg": error_msg,
            "reraise": reraise,
        }
        try:
            while True:
                try:
                    return cast(T | None, safe_execute(func, **adapter_kwargs))
                except TypeError as exc:
                    rejected_kwarg = next(
                        (
                            kwarg_name
                            for kwarg_name in tuple(adapter_kwargs)
                            if _is_unexpected_keyword_error(exc, kwarg_name)
                        ),
                        None,
                    )
                    if rejected_kwarg is None:
                        raise
                    adapter_kwargs.pop(rejected_kwarg)
        except TypeError:
            logger.debug(error_msg, exc_info=True)
            if reraise:
                raise
            return default_return
        except Exception:  # noqa: BLE001 - safe-execute adapters are best effort
            logger.debug(error_msg, exc_info=True)
            if reraise:
                raise
            return default_return
    try:
        return func()
    except Exception:  # noqa: BLE001 - fallback path mirrors safe_execute
        logger.debug(error_msg, exc_info=True)
        if reraise:
            raise
        return default_return


def _thread_start_probe(thread: object) -> tuple[int | None, bool]:
    """Return normalized thread start indicators for compatibility-safe checks.

    Parameters
    ----------
    thread : object
        Thread-like object exposing optional ``ident`` and ``is_alive`` members.

    Returns
    -------
    tuple[int | None, bool]
        Normalized ``(ident, is_alive)`` where ``ident`` is ``None`` unless it
        is an ``int``, and ``is_alive`` is ``False`` unless ``is_alive()``
        returns a real ``bool``.
    """
    thread_ident = getattr(thread, "ident", None)
    if thread_ident is not None and (
        not isinstance(thread_ident, int) or isinstance(thread_ident, bool)
    ):
        thread_ident = None
    is_alive = getattr(thread, "is_alive", None)
    alive_result = False
    if callable(is_alive):
        try:
            alive_result = is_alive()
        except Exception:  # noqa: BLE001 - startup probe must remain best effort
            alive_result = False
    thread_is_alive = alive_result if isinstance(alive_result, bool) else False
    return thread_ident, thread_is_alive


def sanitize_address(address: str | None) -> str | None:
    """Normalize a BLE address or identifier by removing common separators and converting to lowercase.

    Parameters
    ----------
    address : str | None
        Address or identifier to normalize.

    Returns
    -------
    str | None
        Normalized address string with separators removed and lowercased,
        or `None` if `address` is `None` or empty after stripping.
    """
    if address is None:
        return None
    stripped = address.strip()
    if not stripped:
        return None
    cleaned = (
        stripped.replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .replace(" ", "")
        .lower()
    )
    return cleaned if cleaned else None


def _sleep(delay: float) -> None:
    """Pause execution for the specified number of seconds.

    Parameters
    ----------
    delay : float
        Number of seconds to pause; may be fractional (e.g., 0.5).
    """
    time.sleep(delay)


async def with_timeout(
    awaitable: Awaitable[T],
    timeout: float | None,
    label: str,
    timeout_error_factory: Callable[[str, float], Exception] | None = None,
) -> T:
    """Run an awaitable with an optional timeout.

    Parameters
    ----------
    awaitable : Awaitable[T]
        The awaitable to execute.
    timeout : float | None
        Maximum seconds to wait; None means wait indefinitely.
    label : str
        Short operation label passed to the timeout_error_factory when a timeout occurs.
    timeout_error_factory : Callable[[str, float], Exception] | None
        Optional factory that receives (label, timeout)
        and returns the exception to raise when the operation times out. (Default value = None)

    Returns
    -------
    T
        The result produced by the awaitable.

    Raises
    ------
    asyncio.TimeoutError
        If the timeout elapses and no timeout_error_factory is provided.
    Exception
        The exception returned by timeout_error_factory(label, timeout) when a timeout occurs.
    """
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        if timeout_error_factory is None:
            raise
        raise timeout_error_factory(label, timeout) from exc


def resolve_ble_module() -> ModuleType | None:
    """Locate and return the first available BLE-related module for the package.

    Checks for available modules in priority order and returns the first successfully imported module.

    Returns
    -------
    ModuleType | None
        The imported BLE module if found, otherwise `None`.
    """
    for module_name in (
        "meshtastic.ble_interface",
        "meshtastic.interfaces.ble",
    ):
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            # Continue when the target module (or one of its parent packages)
            # is missing; re-raise transitive import failures from inside it.
            missing_name = getattr(exc, "name", None)
            if missing_name is None or (
                missing_name != module_name
                and not module_name.startswith(f"{missing_name}.")
            ):
                raise
            continue
    return None
