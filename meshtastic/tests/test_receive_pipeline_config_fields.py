"""Regression tests for descriptor-driven configuration receive fields."""

import threading
from types import SimpleNamespace

import pytest
from google.protobuf.descriptor import Descriptor

from meshtastic.mesh_interface_runtime.receive_pipeline import (
    LOCAL_CONFIG_FROM_RADIO_FIELDS,
    MODULE_CONFIG_FROM_RADIO_FIELDS,
    ReceivePipeline,
)
from meshtastic.protobuf import config_pb2, localonly_pb2, module_config_pb2


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
@pytest.mark.parametrize(
    "descriptor",
    [config_pb2.Config.DESCRIPTOR, module_config_pb2.ModuleConfig.DESCRIPTOR],
)
def test_inbound_config_wrappers_remain_singular_messages(descriptor: Descriptor) -> None:
    """Receive-copy logic relies on wrapper fields being singular submessages."""
    fields = descriptor.fields
    assert fields
    assert all(field.message_type is not None for field in fields)
    assert all(not field.is_repeated for field in fields)


def _pipeline_with_local_node() -> tuple[ReceivePipeline, SimpleNamespace]:
    local_node = SimpleNamespace(
        localConfig=localonly_pb2.LocalConfig(),
        moduleConfig=localonly_pb2.LocalModuleConfig(),
    )
    interface = SimpleNamespace(
        _node_db_lock=threading.RLock(),
        localNode=local_node,
    )
    return ReceivePipeline(interface), local_node  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_name",
    sorted(
        set(config_pb2.Config.DESCRIPTOR.fields_by_name)
        & set(localonly_pb2.LocalConfig.DESCRIPTOR.fields_by_name)
    ),
)
def test_every_supported_local_config_field_updates_local_cache(field_name: str) -> None:
    """Every wire config field supported by LocalConfig should be copied."""
    pipeline, local_node = _pipeline_with_local_node()
    incoming = config_pb2.Config()
    getattr(incoming, field_name).SetInParent()

    assert pipeline._apply_local_config_from_radio(incoming)  # noqa: SLF001
    assert local_node.localConfig.HasField(field_name)


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_name",
    sorted(
        set(config_pb2.Config.DESCRIPTOR.fields_by_name)
        - set(localonly_pb2.LocalConfig.DESCRIPTOR.fields_by_name)
    ),
)
def test_wire_only_local_config_fields_are_ignored_safely(field_name: str) -> None:
    """Wire-only config sections should not be treated as cache updates."""
    pipeline, _local_node = _pipeline_with_local_node()
    incoming = config_pb2.Config()
    getattr(incoming, field_name).SetInParent()

    assert not pipeline._apply_local_config_from_radio(incoming)  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.parametrize(
    "field_name", [field.name for field in module_config_pb2.ModuleConfig.DESCRIPTOR.fields]
)
def test_every_module_config_field_updates_local_cache(field_name: str) -> None:
    """Every module config wrapper field should copy into the local cache."""
    pipeline, local_node = _pipeline_with_local_node()
    incoming = module_config_pb2.ModuleConfig()
    getattr(incoming, field_name).SetInParent()

    assert pipeline._apply_module_config_from_radio(incoming)  # noqa: SLF001
    assert local_node.moduleConfig.HasField(field_name)
