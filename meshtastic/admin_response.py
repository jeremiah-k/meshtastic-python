"""Typed correlation contracts for ADMIN_APP request/response pairs."""

from __future__ import annotations

from dataclasses import dataclass

from meshtastic.protobuf import admin_pb2, config_pb2, module_config_pb2

_REQUEST_TO_RESPONSE: dict[str, str] = {
    "get_channel_request": "get_channel_response",
    "get_owner_request": "get_owner_response",
    "get_config_request": "get_config_response",
    "get_module_config_request": "get_module_config_response",
    "get_canned_message_module_messages_request": (
        "get_canned_message_module_messages_response"
    ),
    "get_device_metadata_request": "get_device_metadata_response",
    "get_ringtone_request": "get_ringtone_response",
    "get_device_connection_status_request": "get_device_connection_status_response",
    "get_node_remote_hardware_pins_request": (
        "get_node_remote_hardware_pins_response"
    ),
    "get_ui_config_request": "get_ui_config_response",
}


@dataclass(frozen=True, slots=True)
class AdminResponseContract:
    """Expected source, response variant, and optional config subtype."""

    expected_sources: frozenset[int]
    response_variant: str
    response_subtype: str | None = None

    def matches(self, packet: dict[str, object]) -> bool:
        """Return whether ``packet`` satisfies this request's response contract."""
        source = packet.get("from")
        if not isinstance(source, int) or source not in self.expected_sources:
            return False
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return False
        admin = decoded.get("admin")
        if not isinstance(admin, dict):
            return False
        raw = admin.get("raw")
        if not isinstance(raw, admin_pb2.AdminMessage):
            return False
        if raw.WhichOneof("payload_variant") != self.response_variant:
            return False
        if self.response_subtype is None:
            return True
        response = getattr(raw, self.response_variant)
        return response.WhichOneof("payload_variant") == self.response_subtype


def _response_subtype_for_request(
    request_variant: str, request: admin_pb2.AdminMessage
) -> str | None:
    if request_variant == "get_config_request":
        value = int(request.get_config_request)
        fields = config_pb2.Config.DESCRIPTOR.oneofs_by_name["payload_variant"].fields
        return fields[value].name if 0 <= value < len(fields) else None
    if request_variant == "get_module_config_request":
        value = int(request.get_module_config_request)
        fields = module_config_pb2.ModuleConfig.DESCRIPTOR.oneofs_by_name[
            "payload_variant"
        ].fields
        return fields[value].name if 0 <= value < len(fields) else None
    return None


def contract_for_admin_request(
    request: admin_pb2.AdminMessage,
    *,
    destination: int,
    local_node_num: int | None,
) -> AdminResponseContract | None:
    """Build a response contract for a getter request, or ``None`` otherwise."""
    request_variant = request.WhichOneof("payload_variant")
    if request_variant is None:
        return None
    response_variant = _REQUEST_TO_RESPONSE.get(request_variant)
    if response_variant is None:
        return None
    expected_sources = {int(destination)}
    if local_node_num is not None and destination == local_node_num:
        # PhoneAPI-local packets have historically appeared with either zero or
        # the local node number depending on firmware/transport generation.
        expected_sources.add(0)
    return AdminResponseContract(
        expected_sources=frozenset(expected_sources),
        response_variant=response_variant,
        response_subtype=_response_subtype_for_request(request_variant, request),
    )
