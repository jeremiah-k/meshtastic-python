"""Support for logging from power meters/supplies."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Self

from .power_supply import PowerError, PowerMeter, PowerSupply
from .sim import SimPowerSupply
from .stress import PowerStress

if TYPE_CHECKING:
    from .ppk2 import PPK2PowerSupply
    from .riden import RidenPowerSupply

_OPTIONAL_POWERMON_BACKENDS: dict[str, tuple[str, str]] = {
    "PPK2PowerSupply": (".ppk2", "ppk2_api"),
    "RidenPowerSupply": (".riden", "riden"),
}


def _missing_optional_backend_class(
    backend_name: str,
    dependency_name: str,
    import_error: ImportError,
) -> type[PowerSupply]:
    """Return a placeholder backend class that raises a clear dependency error."""

    class _MissingOptionalBackend(PowerSupply):
        def __new__(cls, *args: object, **kwargs: object) -> Self:
            _ = (cls, args, kwargs)
            raise ImportError(
                f"{backend_name} requires optional dependency {dependency_name!r}. "
                "Install Meshtastic with powermon extras to use this backend."
            ) from import_error

    _MissingOptionalBackend.__name__ = backend_name
    _MissingOptionalBackend.__qualname__ = backend_name
    return _MissingOptionalBackend


def __getattr__(name: str) -> Any:
    """Resolve optional backend exports lazily to avoid eager import failures."""
    backend_entry = _OPTIONAL_POWERMON_BACKENDS.get(name)
    if backend_entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, dependency_name = backend_entry
    try:
        module = importlib.import_module(module_name, __name__)
        value = getattr(module, name)
    except ModuleNotFoundError as exc:
        missing_name = getattr(exc, "name", "") or ""
        if missing_name != dependency_name and not missing_name.startswith(
            f"{dependency_name}."
        ):
            raise
        value = _missing_optional_backend_class(name, dependency_name, exc)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return module attributes including lazy optional backend exports."""
    return sorted(set(globals().keys()) | set(_OPTIONAL_POWERMON_BACKENDS.keys()))


__all__ = [
    "PPK2PowerSupply",
    "PowerError",
    "PowerMeter",
    "PowerStress",
    "PowerSupply",
    "RidenPowerSupply",
    "SimPowerSupply",
]
