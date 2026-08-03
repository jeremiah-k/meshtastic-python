"""Configuration presentation and YAML export helpers for the Meshtastic CLI."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

import yaml
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message

import meshtastic.util
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import localonly_pb2


class DescriptorLike(Protocol):
    """Structural descriptor type shared by pure-Python and upb protobuf runtimes."""

    @property
    def fields(self) -> Sequence[FieldDescriptor]:
        """Return fields declared by this message descriptor."""


CONFIG_TRUE_DEFAULTS: set[tuple[str, ...]] = {
    ("bluetooth", "enabled"),
    ("lora", "sx126xRxBoostedGain"),
    ("lora", "txEnabled"),
    ("lora", "usePreset"),
    ("position", "positionBroadcastSmartEnabled"),
    ("security", "serialEnabled"),
}

MODULE_TRUE_DEFAULTS: set[tuple[str, ...]] = {
    ("mqtt", "encryptionEnabled"),
}


def print_config(config: Any, *, camel_case: bool) -> None:
    """Print top-level configuration sections and their writable fields.

    Parameters
    ----------
    config : Any
        Protobuf-like configuration message exposing a ``DESCRIPTOR``.
    camel_case : bool
        Whether field paths should be rendered in camelCase.
    """
    descriptor = config.DESCRIPTOR
    for config_section in descriptor.fields:
        if config_section.name == "version":
            continue
        section_field = descriptor.fields_by_name.get(config_section.name)
        if section_field is None or section_field.message_type is None:
            continue
        print(f"{config_section.name}:")
        names = []
        for field in section_field.message_type.fields:
            field_name = f"{config_section.name}.{field.name}"
            if camel_case:
                field_name = meshtastic.util.snake_to_camel(field_name)
            names.append(field_name)
        for field_name in sorted(names):
            print(f"    {field_name}")


def print_available_config_fields(
    *,
    camel_case: bool,
    aliases: Mapping[str, str],
    display_pref_name: Callable[[str], str],
    local_config_factory: Callable[[], Any] = localonly_pb2.LocalConfig,
    module_config_factory: Callable[[], Any] = localonly_pb2.LocalModuleConfig,
) -> None:
    """Print current local/module fields and compatibility aliases.

    Parameters
    ----------
    camel_case : bool
        Whether config fields should be rendered in camelCase.
    aliases : Mapping[str, str]
        Compatibility aliases mapped to canonical preference paths.
    display_pref_name : Callable[[str], str]
        Formatter used for alias names so legacy CLI casing is preserved.
    local_config_factory : Callable[[], Any]
        Factory for the local-configuration wrapper. This is injectable so the
        historical ``meshtastic.__main__.localonly_pb2`` monkeypatch seam is
        preserved by the compatibility facade.
    module_config_factory : Callable[[], Any]
        Factory for the module-configuration wrapper, with the same compatibility
        injection semantics as ``local_config_factory``.
    """
    print("Local config fields:")
    print_config(local_config_factory(), camel_case=camel_case)
    print("")
    print("Module config fields:")
    print_config(module_config_factory(), camel_case=camel_case)
    if aliases:
        print("")
        print("Compatibility aliases:")
        for alias_name, canonical_name in sorted(aliases.items()):
            print(
                f"    {display_pref_name(alias_name)} -> "
                f"{display_pref_name(canonical_name)}"
            )


def is_repeated_field(field_desc: Any) -> bool:
    """Return whether a protobuf descriptor represents a repeated field."""
    is_repeated = getattr(field_desc, "is_repeated", None)
    if isinstance(is_repeated, bool):
        return is_repeated

    label = getattr(field_desc, "label", None)
    label_repeated = getattr(field_desc, "LABEL_REPEATED", None)
    return label is not None and label == label_repeated


def set_missing_flags_false(
    config_dict: dict[str, Any], true_defaults: set[tuple[str, ...]]
) -> None:
    """Materialize omitted firmware-true boolean defaults as explicit ``False``.

    Parameters
    ----------
    config_dict : dict[str, Any]
        Nested configuration dictionary modified in place.
    true_defaults : set[tuple[str, ...]]
        Key paths whose missing final key should be created with ``False``.
    """
    for path in true_defaults:
        current = config_dict
        for key in path[:-1]:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]
        if path[-1] not in current:
            current[path[-1]] = False


def prefix_base64_bytes_fields(message: Message, values: dict[str, Any]) -> None:
    """Mark every protobuf bytes field in a ``MessageToDict`` mapping as base64."""

    def _field_key(field: FieldDescriptor, mapping: dict[str, Any]) -> str | None:
        json_name: str = getattr(field, "json_name", field.name)
        for candidate in (json_name, field.name):
            if candidate in mapping:
                return candidate
        return None

    def _prefix_bytes(value: Any, *, field_path: str) -> Any:
        if isinstance(value, str):
            return value if value.startswith("base64:") else f"base64:{value}"
        if isinstance(value, list):
            if not all(isinstance(item, str) for item in value):
                raise TypeError(
                    f"Expected base64 strings for repeated bytes field {field_path}"
                )
            return [
                item if item.startswith("base64:") else f"base64:{item}"
                for item in value
            ]
        raise TypeError(f"Expected base64 string for bytes field {field_path}")

    def _walk(
        descriptor: DescriptorLike, mapping: dict[str, Any], *, path: str = ""
    ) -> None:
        for field in descriptor.fields:
            key = _field_key(field, mapping)
            if key is None:
                continue
            value = mapping[key]
            field_path = f"{path}.{field.name}" if path else field.name
            if field.type == FieldDescriptor.TYPE_BYTES:
                mapping[key] = _prefix_bytes(value, field_path=field_path)
                continue
            if field.type != FieldDescriptor.TYPE_MESSAGE:
                continue

            message_type = field.message_type
            if message_type.GetOptions().map_entry:
                value_field = message_type.fields_by_name["value"]
                if not isinstance(value, dict):
                    raise TypeError(
                        f"Expected mapping for protobuf map field {field_path}"
                    )
                if value_field.type == FieldDescriptor.TYPE_BYTES:
                    for map_key, map_value in value.items():
                        value[map_key] = _prefix_bytes(
                            map_value, field_path=f"{field_path}[{map_key!r}]"
                        )
                elif value_field.type == FieldDescriptor.TYPE_MESSAGE:
                    for map_key, map_value in value.items():
                        if not isinstance(map_value, dict):
                            raise TypeError(
                                "Expected mapping for protobuf message map value "
                                f"{field_path}[{map_key!r}]"
                            )
                        _walk(
                            value_field.message_type,
                            map_value,
                            path=f"{field_path}[{map_key!r}]",
                        )
                continue

            if is_repeated_field(field):
                if not isinstance(value, list):
                    raise TypeError(
                        f"Expected list for repeated message field {field_path}"
                    )
                for index, item in enumerate(value):
                    if not isinstance(item, dict):
                        raise TypeError(f"Expected mapping for {field_path}[{index}]")
                    _walk(message_type, item, path=f"{field_path}[{index}]")
            else:
                if not isinstance(value, dict):
                    raise TypeError(f"Expected mapping for message field {field_path}")
                _walk(message_type, value, path=field_path)

    _walk(message.DESCRIPTOR, values)


def prefix_base64_key(
    security: dict[str, Any], normalized_key_map: dict[str, str], camel_name: str
) -> None:
    """Prefix a security key value with ``base64:`` when needed."""
    key = normalized_key_map.get(camel_name)
    if not key:
        return
    value = security.get(key)
    if isinstance(value, str):
        if not value.startswith("base64:"):
            security[key] = "base64:" + value
    elif isinstance(value, list):
        security[key] = [
            (
                "base64:" + item
                if isinstance(item, str) and not item.startswith("base64:")
                else item
            )
            for item in value
        ]


def _converted_section_keys(
    values: dict[str, Any], *, camel_case: bool
) -> dict[str, Any]:
    """Return a shallow copy with top-level section names in requested casing."""
    converted: dict[str, Any] = {}
    for preference, value in values.items():
        key = (
            meshtastic.util.snake_to_camel(preference)
            if camel_case
            else meshtastic.util.camel_to_snake(preference)
        )
        converted[key] = value
    return converted


def export_config(
    interface: MeshInterface,
    *,
    camel_case: bool,
    message_to_dict: Callable[[Message], dict[str, Any]] = MessageToDict,
    prefix_base64_bytes_fields_fn: Callable[
        [Message, dict[str, Any]], None
    ] = prefix_base64_bytes_fields,
    set_missing_flags_false_fn: Callable[
        [dict[str, Any], set[tuple[str, ...]]], None
    ] = set_missing_flags_false,
    config_true_defaults: set[tuple[str, ...]] = CONFIG_TRUE_DEFAULTS,
    module_true_defaults: set[tuple[str, ...]] = MODULE_TRUE_DEFAULTS,
) -> str:
    """Export local node and module configuration as Meshtastic YAML.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface whose local node state should be exported.
    camel_case : bool
        Whether exported configuration section keys use camelCase.
    message_to_dict : Callable[[Message], dict[str, Any]]
        Protobuf-to-dictionary converter. The compatibility facade injects its
        current module-level symbol so monkeypatches continue to take effect.
    prefix_base64_bytes_fields_fn : Callable[[Message, dict[str, Any]], None]
        Bytes-field normalizer injected by the compatibility facade.
    set_missing_flags_false_fn : Callable[[dict[str, Any], set[tuple[str, ...]]], None]
        Missing-default materializer injected by the compatibility facade.
    config_true_defaults : set[tuple[str, ...]]
        Local-config paths whose omitted firmware-true defaults are exported as
        explicit ``False`` values.
    module_true_defaults : set[tuple[str, ...]]
        Module-config paths whose omitted firmware-true defaults are exported as
        explicit ``False`` values.

    Returns
    -------
    str
        YAML text prefixed with the historical configure-file header.
    """
    config_obj: dict[str, Any] = {}

    owner = interface.getLongName()
    owner_short = interface.getShortName()
    channel_url = interface.localNode.getURL()
    my_info = interface.getMyNodeInfo()
    canned_messages = interface.getCannedMessage()
    ringtone = interface.getRingtone()
    position = my_info.get("position") if my_info else None
    latitude = position.get("latitude") if position else None
    longitude = position.get("longitude") if position else None
    altitude = position.get("altitude") if position else None

    if owner:
        config_obj["owner"] = owner
    if owner_short:
        config_obj["owner_short"] = owner_short
    if channel_url:
        config_obj["channelUrl" if camel_case else "channel_url"] = channel_url
    if canned_messages:
        config_obj["canned_messages"] = canned_messages
    if ringtone:
        config_obj["ringtone"] = ringtone
    if latitude is not None or longitude is not None:
        config_obj["location"] = {
            "lat": latitude if latitude is not None else 0.0,
            "lon": longitude if longitude is not None else 0.0,
        }
        if altitude is not None:
            config_obj["location"]["alt"] = altitude

    config = message_to_dict(interface.localNode.localConfig)
    if config:
        prefix_base64_bytes_fields_fn(interface.localNode.localConfig, config)
        set_missing_flags_false_fn(config, config_true_defaults)
        config_obj["config"] = _converted_section_keys(config, camel_case=camel_case)

    module_config = message_to_dict(interface.localNode.moduleConfig)
    if module_config:
        prefix_base64_bytes_fields_fn(interface.localNode.moduleConfig, module_config)
        set_missing_flags_false_fn(module_config, module_true_defaults)
        config_obj["module_config"] = _converted_section_keys(
            module_config, camel_case=camel_case
        )

    return "# start of Meshtastic configure yaml\n" + yaml.dump(config_obj)
