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
from typing import IO, Any, Dict, Optional

MODULE_STATE_DEFAULTS: Dict[str, Any] = {
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
args: Optional[argparse.Namespace]
parser: Optional[argparse.ArgumentParser]
channel_index: Optional[int]
logfile: Optional[IO[str]]
tunnel_instance: Any
camel_case: bool

# Sanity-check: keep MODULE_STATE_DEFAULTS keys and annotations in sync.
# Use globals() to read the module-level annotation dict; pylint does not
# recognise __annotations__ as a valid built-in at module scope, but it IS
# valid Python 3 and globals()["__annotations__"] is the canonical spelling
# that avoids the false-positive lint warning.
_state_keys: frozenset = frozenset(MODULE_STATE_DEFAULTS)
_annotated_state: frozenset = frozenset(
    k for k in globals().get("__annotations__", {}) if k in _state_keys
)
assert _state_keys == _annotated_state, (
    f"Drift between MODULE_STATE_DEFAULTS and type annotations — "
    f"missing annotations: {_state_keys - _annotated_state}, "
    f"missing defaults: {_annotated_state - _state_keys}"
)

reset()
