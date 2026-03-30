"""mesh_interface package - provides MeshInterface and receive pipeline components."""

import sys
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "mesh_interface_module",
    "/home/jeremiah/dev/meshtastic/python/meshtastic/mesh_interface.py",
)
_mesh_interface = importlib.util.module_from_spec(_spec)
sys.modules["meshtastic._mesh_interface_module"] = _mesh_interface
_spec.loader.exec_module(_mesh_interface)  # type: ignore

MeshInterface = _mesh_interface.MeshInterface

from meshtastic.mesh_interface.receive_pipeline import (
    ReceivePipeline,
    _PublicationIntent,
    _LazyMessageDict,
    _FromRadioContext,
    _PacketRuntimeContext,
)
from meshtastic.mesh_interface.send_pipeline import (
    SendPipeline,
    _RequestWaitRuntime,
    PACKET_ID_MASK,
    PACKET_ID_COUNTER_MASK,
    PACKET_ID_RANDOM_MAX,
    PACKET_ID_RANDOM_SHIFT_BITS,
    QUEUE_WAIT_DELAY_SECONDS,
    TelemetryType,
    DEFAULT_TELEMETRY_TYPE,
    VALID_TELEMETRY_TYPES,
    VALID_TELEMETRY_TYPE_SET,
    UNKNOWN_SNR_QUARTER_DB,
    MISSING_NODE_NUM_ERROR_TEMPLATE,
    NODE_NOT_FOUND_IN_DB_ERROR_TEMPLATE,
    NODE_NOT_FOUND_DB_UNAVAILABLE_ERROR_TEMPLATE,
    HEX_NODE_ID_TAIL_CHARS,
    NO_RESPONSE_FIRMWARE_ERROR,
    UNSCOPED_WAIT_REQUEST_ID,
    WAIT_ATTR_POSITION,
    WAIT_ATTR_TELEMETRY,
    WAIT_ATTR_TRACEROUTE,
    WAIT_ATTR_WAYPOINT,
    WAIT_ATTR_NAK,
    LEGACY_UNSCOPED_WAIT_ATTR_BY_PORTNUM,
    RETIRED_WAIT_REQUEST_ID_TTL_SECONDS,
    RESPONSE_WAIT_REQID_ERROR,
)
from meshtastic.mesh_interface.node_view import (
    NodeView,
)

__all__ = [
    "MeshInterface",
    "ReceivePipeline",
    "SendPipeline",
    "NodeView",
]
