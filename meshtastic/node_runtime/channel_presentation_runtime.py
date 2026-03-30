"""User-facing channel/config presentation runtime owner."""

import logging
from typing import TYPE_CHECKING, cast

from google.protobuf.message import Message

from meshtastic.protobuf import channel_pb2
from meshtastic.util import messageToJson, pskToString

from .channel_export_runtime import _NodeChannelExportRuntime

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeChannelPresentationRuntime:
    """Owns channel/info display formatting and presentation orchestration."""

    def __init__(
        self,
        node: "Node",
        *,
        export_runtime: _NodeChannelExportRuntime,
    ) -> None:
        self._node = node
        self._export_runtime = export_runtime

    def _show_channels(self) -> None:
        """Print channels and URL exports preserving historical output behavior."""
        print("Channels:")
        with self._node._channels_lock:  # noqa: SLF001
            channels_snapshot: list[channel_pb2.Channel] = []
            for source_channel in self._node.channels or []:
                copied_channel = channel_pb2.Channel()
                copied_channel.CopyFrom(source_channel)
                channels_snapshot.append(copied_channel)
        if channels_snapshot:
            logger.debug(
                "channel snapshot captured (%d entries): %s",
                len(channels_snapshot),
                [
                    {
                        "index": channel.index,
                        "role": (
                            channel_pb2.Channel.Role.Name(channel.role)
                            if channel.role in channel_pb2.Channel.Role.values()
                            else f"UNKNOWN({channel.role})"
                        ),
                        "name": channel.settings.name if channel.settings else "",
                    }
                    for channel in channels_snapshot
                ],
            )
            for channel in channels_snapshot:
                role_name = (
                    channel_pb2.Channel.Role.Name(channel.role)
                    if channel.role in channel_pb2.Channel.Role.values()
                    else f"UNKNOWN({channel.role})"
                )
                if role_name == "DISABLED":
                    continue
                channel_string = messageToJson(channel.settings)
                print(
                    f"  Index {channel.index}: {role_name} "
                    f"psk={pskToString(channel.settings.psk)} {channel_string}"
                )
        try:
            public_url = self._resolve_export_url(
                channels_snapshot,
                include_all=False,
            )
        except Exception as exc:  # noqa: BLE001 - show_info should remain non-fatal
            logger.warning("Unable to export primary channel URL: %s", exc)
            print("\nPrimary channel URL: unavailable")
            return

        admin_url = public_url
        try:
            admin_url = self._resolve_export_url(
                channels_snapshot,
                include_all=True,
            )
        except Exception as exc:  # noqa: BLE001 - show_info should remain non-fatal
            logger.warning("Unable to export complete channel URL: %s", exc)

        print(f"\nPrimary channel URL: {public_url}")
        if admin_url != public_url:
            print(f"Complete URL (includes all channels): {admin_url}")

    def _resolve_export_url(
        self,
        channels_snapshot: list[channel_pb2.Channel],
        *,
        include_all: bool,
    ) -> str:
        """Resolve URL export path while preserving compatibility with export mocks."""
        get_url = getattr(self._export_runtime, "get_url", None)
        if callable(get_url):
            return cast(str, get_url(include_all=include_all))
        return self._export_runtime._get_url_from_snapshot(
            channels_snapshot,
            include_all=include_all,
        )

    def _show_info(self) -> None:
        """Print local/module preferences and current channel presentation."""
        local_config_snapshot: Message | None = None
        module_config_snapshot: Message | None = None
        node_db_lock = getattr(self._node, "_node_db_lock", None)
        if node_db_lock is not None and hasattr(node_db_lock, "__enter__"):
            with node_db_lock:
                (
                    local_config_snapshot,
                    module_config_snapshot,
                ) = self._snapshot_configs()
        else:
            local_config_snapshot, module_config_snapshot = self._snapshot_configs()

        prefs = ""
        if local_config_snapshot:
            prefs = messageToJson(local_config_snapshot, multiline=True)
        print(f"Preferences: {prefs}\n")
        prefs = ""
        if module_config_snapshot:
            prefs = messageToJson(module_config_snapshot, multiline=True)
        print(f"Module preferences: {prefs}\n")
        self._show_channels()

    def _snapshot_configs(self) -> tuple[Message | None, Message | None]:
        """Return detached snapshots of local/module configs when present."""
        local_config_snapshot: Message | None = None
        module_config_snapshot: Message | None = None
        if self._node.localConfig is not None:
            local_config_snapshot = cast(Message, type(self._node.localConfig)())
            local_config_snapshot.CopyFrom(self._node.localConfig)
        if self._node.moduleConfig is not None:
            module_config_snapshot = cast(Message, type(self._node.moduleConfig)())
            module_config_snapshot.CopyFrom(self._node.moduleConfig)
        return local_config_snapshot, module_config_snapshot
