"""Internal response-bookkeeping types shared by runtime components.

The public aliases remain available from :mod:`meshtastic`; this leaf module
exists so internal request runtimes do not depend on package-root implementation.
"""

from typing import Any, Callable, NamedTuple

__all__ = ("ResponseCallback", "ResponseHandler")

ResponseCallback = Callable[[dict[str, Any]], Any]


class ResponseHandler(NamedTuple):
    """A pending response callback, waiting for a response to one of our messages."""

    callback: ResponseCallback
    ackPermitted: bool = False


# Registration timestamps remain out-of-band so the historical two-field tuple
# shape stays compatible. NamedTuple records its defining module in pickle and
# introspection metadata, so preserve the historical public lookup path after
# moving the implementation into this private leaf module.
ResponseHandler.__module__ = "meshtastic"
