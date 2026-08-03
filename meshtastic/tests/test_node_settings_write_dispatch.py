"""Focused tests for schema-driven settings write dispatch."""

from unittest.mock import MagicMock

import pytest
from google.protobuf.message import Message

from meshtastic.node_runtime.settings_runtime.message import _NodeSettingsMessageBuilder
from meshtastic.protobuf import admin_pb2, atak_pb2, localonly_pb2


def _builder() -> tuple[_NodeSettingsMessageBuilder, MagicMock]:
    node = MagicMock(spec=["localConfig", "moduleConfig", "_raise_interface_error"])
    node.localConfig = localonly_pb2.LocalConfig()
    node.moduleConfig = localonly_pb2.LocalModuleConfig()
    return _NodeSettingsMessageBuilder(node), node


def _shared_field_names(setter_name: str, source: Message) -> set[str]:
    setter = admin_pb2.AdminMessage.DESCRIPTOR.fields_by_name[setter_name]
    assert setter.message_type is not None
    return set(setter.message_type.fields_by_name) & set(source.DESCRIPTOR.fields_by_name)


@pytest.mark.unit
def test_write_dispatch_matches_admin_and_local_config_schema_intersection() -> None:
    """Writable sections should follow both admin wire schema and local cache schema."""
    builder, node = _builder()

    dispatch = builder._write_config_dispatch()  # noqa: SLF001

    expected_local = _shared_field_names("set_config", node.localConfig)
    expected_module = _shared_field_names("set_module_config", node.moduleConfig)
    assert set(dispatch) == expected_local | expected_module
    assert all(dispatch[name][0] == "set_config" for name in expected_local)
    assert all(dispatch[name][0] == "set_module_config" for name in expected_module)
    assert "version" not in dispatch


@pytest.mark.unit
@pytest.mark.parametrize(
    ("config_name", "field_name", "value"),
    [("tak", "team", atak_pb2.Team.Red), ("mesh_beacon", "flags", 5)],
)
def test_new_module_sections_build_write_messages(
    config_name: str, field_name: str, value: int
) -> None:
    """New module sections received/exported by the client must also be writable."""
    builder, node = _builder()
    source_config = getattr(node.moduleConfig, config_name)
    setattr(source_config, field_name, value)

    message = builder.build_write_message(config_name)

    assert message.HasField("set_module_config")
    populated = {field.name for field, _ in message.set_module_config.ListFields()}
    assert config_name in populated
    assert getattr(message.set_module_config, config_name) == source_config


@pytest.mark.unit
def test_unknown_write_section_remains_invalid() -> None:
    """Descriptor-driven dispatch must not make arbitrary names writable."""
    builder, node = _builder()
    node._raise_interface_error.side_effect = RuntimeError

    with pytest.raises(RuntimeError):
        builder.validate_config_name("not_a_config_section")
