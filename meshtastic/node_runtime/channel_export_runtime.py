"""Channel export/hash and primary-channel mutation runtime owner."""

import base64
import logging
from typing import TYPE_CHECKING, Any

from meshtastic.protobuf import apponly_pb2, channel_pb2, localonly_pb2
from meshtastic.util import fromPSK, generate_channel_hash

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)


class _NodeChannelExportRuntime:
    """Owns channel URL export, channel-hash export, and primary PSK mutation."""

    def __init__(self, node: "Node") -> None:
        self._node = node

    def _snapshot_channels(self) -> list[channel_pb2.Channel]:
        """Return detached channel snapshots captured under the channel lock."""
        with self._node._channels_lock:  # noqa: SLF001
            snapshot: list[channel_pb2.Channel] = []
            for source_channel in self._node.channels or []:
                copied_channel = channel_pb2.Channel()
                copied_channel.CopyFrom(source_channel)
                snapshot.append(copied_channel)
            return snapshot

    def _snapshot_local_config(self) -> localonly_pb2.LocalConfig:
        """Return detached localConfig snapshot for consistent field checks/copies."""
        config_snapshot = localonly_pb2.LocalConfig()
        config_snapshot.CopyFrom(self._node.localConfig)
        return config_snapshot

    def get_url(self, *, include_all: bool = True) -> str:
        """Build channel URL export with preserved includeAll and LoRa semantics."""
        channel_set = apponly_pb2.ChannelSet()
        channels_snapshot = self._snapshot_channels()
        if channels_snapshot:
            for channel in channels_snapshot:
                if channel.role == channel_pb2.Channel.Role.PRIMARY or (
                    include_all and channel.role == channel_pb2.Channel.Role.SECONDARY
                ):
                    channel_set.settings.append(channel.settings)

        local_config_snapshot = self._snapshot_local_config()
        if not local_config_snapshot.HasField("lora"):
            self._node.requestConfig(
                local_config_snapshot.DESCRIPTOR.fields_by_name["lora"]
            )
            wait_for_config = getattr(self._node, "waitForConfig", None)
            if callable(wait_for_config):
                wait_for_config(attribute="lora")
            local_config_snapshot = self._snapshot_local_config()
            if not local_config_snapshot.HasField("lora"):
                self._node._raise_interface_error(  # noqa: SLF001
                    "LoRa config must be loaded before exporting a channel URL"
                )
        channel_set.lora_config.CopyFrom(local_config_snapshot.lora)
        serialized_channel_set = channel_set.SerializeToString()
        encoded = base64.urlsafe_b64encode(serialized_channel_set).decode("ascii")
        encoded = encoded.rstrip("=")
        return f"https://meshtastic.org/e/#{encoded}"

    def get_channels_with_hash(self) -> list[dict[str, Any]]:
        """Return index/role/name/hash descriptors for current channel snapshot."""
        result: list[dict[str, Any]] = []
        channels_snapshot = self._snapshot_channels()
        if channels_snapshot:
            for channel in channels_snapshot:
                settings = getattr(channel, "settings", None)
                name = getattr(settings, "name", "")
                psk = getattr(settings, "psk", b"")
                has_name = bool(name)
                has_psk = bool(psk)
                hash_value = (
                    generate_channel_hash(name, psk) if has_name and has_psk else None
                )
                result.append(
                    {
                        "index": channel.index,
                        "role": channel_pb2.Channel.Role.Name(channel.role),
                        "name": name if has_name else "",
                        "hash": hash_value,
                    }
                )
        return result

    def turn_off_encryption_on_primary_channel(self) -> None:
        """Disable primary-channel encryption and persist updated channel state."""
        primary_snapshot: channel_pb2.Channel | None = None
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if not channels:
                self._node._raise_interface_error(
                    "Error: No channels have been read"
                )  # noqa: SLF001
            for channel in channels:
                if channel.role == channel_pb2.Channel.Role.PRIMARY:
                    primary_snapshot = channel_pb2.Channel()
                    primary_snapshot.CopyFrom(channel)
                    primary_snapshot.settings.psk = fromPSK("none")
                    break
            if primary_snapshot is None:
                self._node._raise_interface_error(
                    "Error: No primary channel found"
                )  # noqa: SLF001
        logger.info("Writing modified channels to device")
        self._node._write_channel_snapshot(primary_snapshot)  # noqa: SLF001
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if not channels:
                logger.warning(
                    "Primary channel write succeeded but local channel cache is unavailable; reload channels to refresh local state."
                )
                return
            for channel in channels:
                if channel.index == primary_snapshot.index:
                    channel.CopyFrom(primary_snapshot)
                    return
            logger.warning(
                "Primary channel write succeeded but local channel index %s is unavailable; invalidating local channel cache.",
                primary_snapshot.index,
            )
            self._node.channels = None
