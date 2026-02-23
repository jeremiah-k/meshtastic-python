"""Module-level singleton namespace.

The Global object is gone, as are all its setters and getters. Instead the
module itself is the singleton namespace, which can be imported into
whichever module is used. The associated tests have also been removed,
since we now rely on built in Python mechanisms.

This is intended to make the Python read more naturally, and to make the
intention of the code clearer and more compact. It is merely a sticking
plaster over the use of shared mt_config, but the coupling issues will be dealt
with rather more easily once the code is simplified by this change.
"""

import argparse
import inspect
import os
import sys
import warnings
from typing import IO, Any

# NOTE: Any additional public annotated module-level constants that are not
# reset-managed state must either be prefixed with "_" or excluded from the
# _annotated_state filter below.
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
    """Reset module-level state variables to their defined default values.

    Defaults are applied from MODULE_STATE_DEFAULTS to restore the module to its initial pristine state.

    Returns
    -------
    None
        This function does not return a value.
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
_state_keys: frozenset[str] = frozenset(MODULE_STATE_DEFAULTS)
_module_annotations: dict[str, Any] = inspect.get_annotations(
    sys.modules[__name__], eval_str=False
)
_annotated_state: frozenset[str] = frozenset(
    key
    for key in _module_annotations
    if not key.startswith("_") and key != "MODULE_STATE_DEFAULTS"
)
if not _module_annotations:
    warnings.warn(
        "inspect.get_annotations() returned no module annotations; skipping "
        "mt_config state drift validation.",
        RuntimeWarning,
        stacklevel=1,
    )
elif _state_keys != _annotated_state:
    drift_message = (
        "Drift between MODULE_STATE_DEFAULTS and type annotations — "
        f"missing annotations: {_state_keys - _annotated_state}, "
        f"missing defaults: {_annotated_state - _state_keys}"
    )
    if os.getenv("CI_ASSERT_MODULE_STATE_DRIFT", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        raise AssertionError(drift_message)  # noqa: TRY003
    warnings.warn(drift_message, RuntimeWarning, stacklevel=1)

reset()
