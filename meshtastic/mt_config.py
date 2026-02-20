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
from typing import IO, Any, Optional

MODULE_STATE_DEFAULTS: dict[str, Any] = {
    "args": None,
    "parser": None,
    "channel_index": None,
    "logfile": None,
    "tunnelInstance": None,
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
tunnelInstance: Any
camel_case: bool

reset()
