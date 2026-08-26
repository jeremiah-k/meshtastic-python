"""Configuration presentation and YAML export helpers for the Meshtastic CLI."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import Any, Protocol

import yaml
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError, Message

import meshtastic.util
from meshtastic.cli.context import CliExit, _terminate_cli
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import clientonly_pb2, localonly_pb2


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
    config_dict: MutableMapping[str, Any], true_defaults: set[tuple[str, ...]]
) -> None:
    """Materialize omitted firmware-true boolean defaults as explicit ``False``.

    Parameters
    ----------
    config_dict : MutableMapping[str, Any]
        Nested configuration mapping modified in place.
    true_defaults : set[tuple[str, ...]]
        Key paths whose missing final key should be created with ``False``.
    """
    for path in true_defaults:
        current = config_dict
        for key in path[:-1]:
            if key not in current or not isinstance(current[key], MutableMapping):
                current[key] = {}
            current = current[key]
        if path[-1] not in current:
            current[path[-1]] = False


def prefix_base64_bytes_fields(
    message: Message, values: MutableMapping[str, Any]
) -> None:
    """Mark every protobuf bytes field in a ``MessageToDict`` mapping as base64."""

    def _field_key(
        field: FieldDescriptor, mapping: MutableMapping[str, Any]
    ) -> str | None:
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
        descriptor: DescriptorLike, mapping: MutableMapping[str, Any], *, path: str = ""
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
                if not isinstance(value, MutableMapping):
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
                        if not isinstance(map_value, MutableMapping):
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
                    if not isinstance(item, MutableMapping):
                        raise TypeError(f"Expected mapping for {field_path}[{index}]")
                    _walk(message_type, item, path=f"{field_path}[{index}]")
            else:
                if not isinstance(value, MutableMapping):
                    raise TypeError(f"Expected mapping for message field {field_path}")
                _walk(message_type, value, path=field_path)

    _walk(message.DESCRIPTOR, values)


# COMPAT_STABLE_SHIM: implementation backing meshtastic.__main__._prefix_base64_key.
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
    values: Mapping[str, Any], *, camel_case: bool
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
    message_to_dict: Callable[[Message], MutableMapping[str, Any]] = MessageToDict,
    prefix_base64_bytes_fields_fn: Callable[
        [Message, MutableMapping[str, Any]], None
    ] = prefix_base64_bytes_fields,
    set_missing_flags_false_fn: Callable[
        [MutableMapping[str, Any], set[tuple[str, ...]]], None
    ] = set_missing_flags_false,
    config_true_defaults: set[tuple[str, ...]] | None = None,
    module_true_defaults: set[tuple[str, ...]] | None = None,
) -> str:
    """Export local node and module configuration as Meshtastic YAML.

    Parameters
    ----------
    interface : MeshInterface
        Connected interface whose local node state should be exported.
    camel_case : bool
        Whether exported configuration section keys use camelCase.
    message_to_dict : Callable[[Message], MutableMapping[str, Any]]
        Protobuf-to-dictionary converter. The compatibility facade injects its
        current module-level symbol so monkeypatches continue to take effect.
    prefix_base64_bytes_fields_fn : Callable[[Message, MutableMapping[str, Any]], None]
        Bytes-field normalizer injected by the compatibility facade.
    set_missing_flags_false_fn : Callable[[MutableMapping[str, Any], set[tuple[str, ...]]], None]
        Missing-default materializer injected by the compatibility facade.
    config_true_defaults : set[tuple[str, ...]] | None
        Optional local-config paths whose omitted firmware-true defaults are
        exported as explicit ``False`` values.
    module_true_defaults : set[tuple[str, ...]] | None
        Optional module-config paths whose omitted firmware-true defaults are
        exported as explicit ``False`` values.

    Returns
    -------
    str
        YAML text prefixed with the historical configure-file header.
    """
    config_true_defaults = (
        CONFIG_TRUE_DEFAULTS if config_true_defaults is None else config_true_defaults
    )
    module_true_defaults = (
        MODULE_TRUE_DEFAULTS if module_true_defaults is None else module_true_defaults
    )

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
    prefix_base64_bytes_fields_fn(interface.localNode.localConfig, config)
    set_missing_flags_false_fn(config, config_true_defaults)
    if config:
        config_obj["config"] = _converted_section_keys(config, camel_case=camel_case)

    module_config = message_to_dict(interface.localNode.moduleConfig)
    prefix_base64_bytes_fields_fn(interface.localNode.moduleConfig, module_config)
    set_missing_flags_false_fn(module_config, module_true_defaults)
    if module_config:
        config_obj["module_config"] = _converted_section_keys(
            module_config, camel_case=camel_case
        )

    return "# start of Meshtastic configure yaml\n" + yaml.dump(config_obj)


def export_profile(
    interface: MeshInterface,
) -> bytes:
    """Export local node configuration as a binary DeviceProfile (.cfg).

    Parameters
    ----------
    interface : MeshInterface
        Connected interface whose local node state should be exported.

    Returns
    -------
    bytes
        Serialized ``clientonly_pb2.DeviceProfile`` suitable for ``--configure``.

    Notes
    -----
    Mirrors :func:`export_config` data collection. Canned messages, ringtone,
    and fixed position are included only when populated; local and module
    configurations are always copied so firmware defaults round-trip.
    """
    profile = clientonly_pb2.DeviceProfile()
    owner = interface.getLongName()
    owner_short = interface.getShortName()
    channel_url = interface.localNode.getURL()
    canned_messages = interface.getCannedMessage()
    ringtone = interface.getRingtone()
    my_info = interface.getMyNodeInfo()
    position = my_info.get("position") if my_info else None
    latitude = position.get("latitude") if position else None
    longitude = position.get("longitude") if position else None
    altitude = position.get("altitude") if position else None

    if owner:
        profile.long_name = owner
    if owner_short:
        profile.short_name = owner_short
    if channel_url:
        profile.channel_url = channel_url
    if canned_messages:
        profile.canned_messages = canned_messages
    if ringtone:
        profile.ringtone = ringtone
    if latitude is not None or longitude is not None or altitude is not None:
        profile.fixed_position.latitude_i = int(round((latitude or 0.0) * 1e7))
        profile.fixed_position.longitude_i = int(round((longitude or 0.0) * 1e7))
        profile.fixed_position.altitude = int(altitude or 0)
    profile.config.CopyFrom(interface.localNode.localConfig)
    profile.module_config.CopyFrom(interface.localNode.moduleConfig)
    return profile.SerializeToString()


def parse_profile_bytes(raw: bytes) -> clientonly_pb2.DeviceProfile:
    """Parse raw bytes as a DeviceProfile protobuf.

    Parameters
    ----------
    raw : bytes
        Candidate serialized DeviceProfile payload.

    Returns
    -------
    clientonly_pb2.DeviceProfile
        Parsed profile.

    Raises
    ------
    ValueError
        If the payload is not a parsable DeviceProfile.
    """
    profile = clientonly_pb2.DeviceProfile()
    try:
        profile.ParseFromString(raw)
    except DecodeError as exc:
        raise ValueError(f"invalid DeviceProfile payload: {exc}") from exc
    return profile


_YAML_ALLOWED_CONTROL_CHARS = frozenset("\t\n\r")


def has_yaml_forbidden_control_chars(text: str) -> bool:
    """Detect control characters that YAML forbids inside decoded bytes.

    Parameters
    ----------
    text : str
        Decoded textual candidate for a YAML configuration document.

    Returns
    -------
    bool
        True when the text contains C0 control characters YAML rejects,
        indicating binary content rather than a YAML document.
    """
    return any(
        ord(char) < 32 and char not in _YAML_ALLOWED_CONTROL_CHARS or ord(char) == 127
        for char in text
    )


def profile_to_configuration(
    profile: clientonly_pb2.DeviceProfile,
) -> dict[str, Any]:
    """Convert a DeviceProfile into the equivalent YAML configure document.

    Parameters
    ----------
    profile : clientonly_pb2.DeviceProfile
        Profile to adapt, typically parsed from a binary ``.cfg`` file.

    Returns
    -------
    dict[str, Any]
        Mapping using the same keys as YAML exports, so binary profiles flow
        through the standard configure pipeline unchanged.
    """
    configuration: dict[str, Any] = {}
    if profile.long_name:
        configuration["owner"] = profile.long_name
    if profile.short_name:
        configuration["owner_short"] = profile.short_name
    if profile.channel_url:
        configuration["channel_url"] = profile.channel_url
    if profile.canned_messages:
        configuration["canned_messages"] = profile.canned_messages
    if profile.ringtone:
        configuration["ringtone"] = profile.ringtone
    fixed = profile.fixed_position
    if fixed.latitude_i or fixed.longitude_i or fixed.altitude:
        configuration["location"] = {
            "lat": fixed.latitude_i / 1e7,
            "lon": fixed.longitude_i / 1e7,
            "alt": fixed.altitude,
        }
    config = MessageToDict(profile.config)
    if config:
        configuration["config"] = _converted_section_keys(config, camel_case=False)
    module_config = MessageToDict(profile.module_config)
    if module_config:
        configuration["module_config"] = _converted_section_keys(
            module_config, camel_case=False
        )
    return configuration


EXPORT_FILE_MODE: int = 0o600


def resolve_export_format(fmt: str, destination: str) -> str:
    """Resolve the effective export format for a destination.

    Parameters
    ----------
    fmt : str
        Requested format: ``auto``, ``yaml``, ``binary``, or ``protobuf``.
    destination : str
        Export destination path or ``-`` for stdout.

    Returns
    -------
    str
        Either ``yaml`` or ``binary``.
    """
    if fmt in ("binary", "protobuf"):
        return "binary"
    if fmt == "yaml":
        return "yaml"
    lowered = destination.lower()
    if lowered.endswith((".cfg", ".bin")):
        return "binary"
    return "yaml"


def write_binary_profile(
    export_path: str,
    get_payload: Callable[[], bytes],
    cli_exit: CliExit,
    cli_print: Callable[[str], None],
) -> None:
    """Write a binary DeviceProfile payload to *export_path*.

    Parameters
    ----------
    export_path : str
        Destination path, or ``-`` to indicate stdout (which binary payloads
        cannot target — that case terminates the CLI with a clear error).
    get_payload : Callable[[], bytes]
        Zero-argument callable that returns the serialized DeviceProfile
        bytes; invoked lazily so termination happens before the device
        round-trip when stdout is requested.
    cli_exit : CliExit
        Entrypoint-owned exit seam invoked for unrecoverable errors.
    cli_print : Callable[[str], None]
        Entrypoint-owned print seam for the final success status line.
    """
    if export_path == "-":
        # Binary payloads are meaningless on a text console; refuse rather
        # than spew protobuf bytes into a terminal or capture file.
        _terminate_cli(
            cli_exit,
            "ERROR: Binary export requires a file path; use --export-format yaml "
            "for stdout.",
            1,
        )
    payload = get_payload()
    try:
        descriptor = os.open(
            export_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            EXPORT_FILE_MODE,
        )
        try:
            fchmod = getattr(os, "fchmod", None)
            if callable(fchmod):
                fchmod(descriptor, EXPORT_FILE_MODE)
            with os.fdopen(descriptor, "wb") as output_file:
                descriptor = -1
                output_file.write(payload)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        _terminate_cli(cli_exit, f"ERROR: Failed to write config file: {exc}", 1)
    cli_print(f"Exported configuration to {export_path}")
