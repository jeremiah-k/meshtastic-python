"""
Globals singleton class.

The Global object is gone, as are all its setters and getters. Instead the
module itself is the singleton namespace, which can be imported into
whichever module is used. The associated tests have also been removed,
since we now rely on built in Python mechanisms.

This is intended to make the Python read more naturally, and to make the
intention of the code clearer and more compact. It is merely a sticking
plaster over the use of shared mt_config, but the coupling issues wil be dealt
with rather more easily once the code is simplified by this change.

"""

import argparse
import inspect
import sys
from typing import IO, Any

MODULE_STATE_DEFAULTS: dict[str, Any] = {
    "args": None,
    "parser": None,
    "channel_index": None,
    "logfile": None,
    "tunnel_instance": None,
    # TODO: to migrate to camel_case for v1.3 change this value to True
    "camel_case": False,
}


def reset() -> None:
    """
    Reset the module-level namespace to its initial pristine state.

    Uses MODULE_STATE_DEFAULTS as the single source of truth for tracked
    module state so declarations and reset behavior cannot silently drift.
    """
    module_globals = globals()
    for name, default in MODULE_STATE_DEFAULTS.items():
        module_globals[name] = default


# Declared module state managed via reset().
args: argparse.Namespace | None
parser: argparse.ArgumentParser | None
channel_index: int | None
logfile: IO[str] | None
tunnel_instance: Any
camel_case: bool

# Sanity-check: keep MODULE_STATE_DEFAULTS keys and annotations in sync.
# Python 3.14+ can defer module annotations; inspect.get_annotations() handles
# eager and deferred annotation models consistently.
_state_keys: frozenset = frozenset(MODULE_STATE_DEFAULTS)
try:
    _module_annotations: dict[str, Any] = inspect.get_annotations(
        sys.modules[__name__], eval_str=False
    )
except (AttributeError, NameError, TypeError):
    _module_annotations = globals().get("__annotations__", {})
_annotated_state: frozenset = frozenset(
    k for k in _module_annotations if k in _state_keys
)
if _module_annotations and _state_keys != _annotated_state:
    raise AssertionError(
        f"Drift between MODULE_STATE_DEFAULTS and type annotations — "
        f"missing annotations: {_state_keys - _annotated_state}, "
        f"missing defaults: {_annotated_state - _state_keys}"
    )

reset()
