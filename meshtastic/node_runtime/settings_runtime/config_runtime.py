"""Settings request/write orchestration and callback policy."""

import logging
from typing import TYPE_CHECKING

from google.protobuf.descriptor import FieldDescriptor

from meshtastic.node_runtime.admin_wait import (
    _scoped_request_id,
    _send_admin_with_ack_scope,
    _wait_for_admin_ack,
)
from meshtastic.node_runtime.settings_runtime.message import (  # pylint: disable=no-name-in-module
    _NodeSettingsMessageBuilder,
)

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeSettingsRuntime:
    """Owns settings request/write orchestration and callback policy."""

    def __init__(
        self,
        node: "Node",
        *,
        message_builder: _NodeSettingsMessageBuilder,
    ) -> None:
        self._node = node
        self._message_builder = message_builder

    def request_config(
        self,
        config_type: int | FieldDescriptor,
        *,
        admin_index: int | None = None,
    ) -> None:
        """Send one settings request and register its response application.

        The response handler runs for local and remote nodes alike: without it
        the device's config response is never correlated, so a cleared section
        (e.g. a pre-verification refresh) silently stays at defaults and any
        subsequent comparison reads manufactured values instead of device
        state. Scoped requests wait for correlated completion. The historical
        local compatibility path has no scoped wait bookkeeping, so it returns
        after registering the response callback and applies that response
        asynchronously when it arrives.
        """
        if self._node is not self._node.iface.localNode:
            logger.info(
                "Requesting current config from remote node (this can take a while)."
            )
        on_response = self._node.onResponseRequestSettings

        message = self._message_builder.build_request_message(config_type)
        request = _send_admin_with_ack_scope(
            self._node,
            message,
            scope_ack=on_response is not None,
            wantResponse=True,
            onResponse=on_response,
            adminIndex=admin_index,
        )
        # In noProto mode, _send_admin legitimately returns None (no actual sending)
        if request is None and not getattr(self._node, "noProto", False):
            self._node._raise_interface_error(
                f"requestConfig failed: admin message not started (admin_index={admin_index})"
            )
        if on_response is not None and request is not None:
            if (
                self._node is self._node.iface.localNode
                and _scoped_request_id(self._node, request) is None
            ):
                # A want_response settings request to the local node completes
                # through its correlated data response, not a Routing ACK.
                # Without scoped wait bookkeeping there is no bounded,
                # correlated wait to run, so fall back to the registered
                # response handler alone instead of the legacy interface ACK
                # wait, which would block on an acknowledgment the firmware
                # never sends for want_response admin requests.
                return
            _wait_for_admin_ack(self._node, request)

    def _validate_write_configs_loaded(self, config_name: str) -> None:
        """Preserve historical writeConfig loaded-state behavior.

        Historical behavior only required that *some* local/module config had
        been loaded before writes. Keep that compatibility for configure flows
        that intentionally write empty/default sections.
        """
        config_entry = self._message_builder.get_write_config_entry(config_name)
        if config_entry is None:
            self._node._raise_interface_error(  # noqa: SLF001
                f"Error: No valid config with name {config_name}"
            )

        _, source_config = config_entry
        if len(source_config.ListFields()) > 0:
            return
        if (
            len(self._node.localConfig.ListFields()) > 0
            or len(self._node.moduleConfig.ListFields()) > 0
        ):
            logger.debug(
                "Writing %s with empty payload to preserve historical compatibility.",
                config_name,
            )
            return
        self._node._raise_interface_error(  # noqa: SLF001
            "Error: No config has been read. "
            "Request config from the device before writing."
        )

    def write_config(self, config_name: str) -> None:
        """Send one settings write with preserved callback selection."""
        self._message_builder.validate_config_name(config_name)
        self._validate_write_configs_loaded(config_name)
        message = self._message_builder.build_write_message(config_name)
        logger.debug("Sending write: %s", config_name)
        self._node.ensureSessionKey()
        on_response = (
            None if self._node is self._node.iface.localNode else self._node.onAckNak
        )
        request = _send_admin_with_ack_scope(
            self._node,
            message,
            scope_ack=on_response is not None,
            onResponse=on_response,
        )
        # In noProto mode, _send_admin legitimately returns None (no actual sending)
        if request is None and not getattr(self._node, "noProto", False):
            self._node._raise_interface_error(
                f"writeConfig failed: admin message not started (config_name={config_name})"
            )
        if on_response is not None and request is not None:
            _wait_for_admin_ack(self._node, request)
        logger.debug("Config write completed: %s", config_name)
