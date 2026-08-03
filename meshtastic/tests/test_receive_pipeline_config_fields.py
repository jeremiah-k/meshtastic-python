"""Regression tests for descriptor-driven configuration receive fields."""

import threading
from types import SimpleNamespace

import pytest

from meshtastic.mesh_interface_runtime.receive_pipeline import (
    LOCAL_CONFIG_FROM_RADIO_FIELDS,
    MODULE_CONFIG_FROM_RADIO_FIELDS,
    ReceivePipeline,
)
from meshtastic.protobuf import atak_pb2, config_pb2, localonly_pb2, module_config_pb2


@pytest.mark.unit
def test_receive_field_lists_follow_active_protobuf_descriptors() -> None:
    """Every inbound config field in the active schema should be considered."""
    assert LOCAL_CONFIG_FROM_RADIO_FIELDS == tuple(
        field.name for field in config_pb2.Config.DESCRIPTOR.fields
    )
    assert MODULE_CONFIG_FROM_RADIO_FIELDS == tuple(
        field.name for field in module_config_pb2.ModuleConfig.DESCRIPTOR.fields
    )


@pytest.mark.unit
@pytest.mark.parametrize("field_name", ["tak", "mesh_beacon"])
def test_new_module_config_fields_update_local_cache(field_name: str) -> None:
    """Recently added module config sections must not be silently dropped."""
    local_node = SimpleNamespace(moduleConfig=localonly_pb2.LocalModuleConfig())
    interface = SimpleNamespace(
        _node_db_lock=threading.RLock(),
        localNode=local_node,
    )
    pipeline = ReceivePipeline(interface)  # type: ignore[arg-type]
    incoming = module_config_pb2.ModuleConfig()

    if field_name == "tak":
        incoming.tak.team = atak_pb2.Team.Red
    else:
        incoming.mesh_beacon.broadcast_message = "schema-driven"

    assert pipeline._apply_module_config_from_radio(incoming)  # noqa: SLF001
    assert getattr(local_node.moduleConfig, field_name) == getattr(incoming, field_name)
