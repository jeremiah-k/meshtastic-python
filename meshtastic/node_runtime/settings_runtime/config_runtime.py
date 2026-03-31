"""Settings request/write orchestration and callback policy."""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from google.protobuf.descriptor import FieldDescriptor

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

    def requestConfig(
        self,
        config_type: int | FieldDescriptor,
        *,
        admin_index: int | None = None,
    ) -> None:
        """Send one settings request with preserved local/remote wait semantics."""
        if self._node is self._node.iface.localNode:
            on_response: Callable[[dict[str, Any]], Any] | None = None
        else:
            on_response = self._node.onResponseRequestSettings
            logger.info(
                "Requesting current config from remote node (this can take a while)."
            )

        message = self._message_builder.build_request_message(config_type)
        request = self._node._send_admin(  # noqa: SLF001
            message,
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
            self._node.iface.waitForAckNak()

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

    def writeConfig(self, config_name: str) -> None:
        """Send one settings write with preserved callback selection."""
        self._message_builder.validate_config_name(config_name)
        self._validate_write_configs_loaded(config_name)
        message = self._message_builder.build_write_message(config_name)
        logger.debug("Sending write: %s", config_name)
        self._node.ensureSessionKey()
        on_response = (
            None if self._node is self._node.iface.localNode else self._node.onAckNak
        )
        request = self._node._send_admin(  # noqa: SLF001
            message, onResponse=on_response
        )
        # In noProto mode, _send_admin legitimately returns None (no actual sending)
        if request is None and not getattr(self._node, "noProto", False):
            self._node._raise_interface_error(
                f"writeConfig failed: admin message not started (config_name={config_name})"
            )
        if on_response is not None and request is not None:
            self._node.iface.waitForAckNak()
        logger.debug("Config write completed: %s", config_name)
