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


def reset() -> None:
    """
    Reset the module-level namespace to its initial pristine state.
    
    Sets the globals `args`, `parser`, `channel_index`, `logfile`, and `tunnelInstance` to `None` and `camel_case` to `False`.
    """
    # pylint: disable=W0603
    global args, parser, channel_index, logfile, tunnelInstance, camel_case
    args = None
    parser = None
    channel_index = None
    logfile = None
    tunnelInstance = None
    # TODO: to migrate to camel_case for v1.3 change this value to True
    camel_case = False


# These assignments are used instead of calling reset()
# purely to shut pylint up.
args: Optional[argparse.Namespace] = None
parser: Optional[argparse.ArgumentParser] = None
channel_index: Optional[int] = None
logfile: Optional[IO[str]] = None
tunnelInstance: Optional[Any] = None
camel_case: bool = False