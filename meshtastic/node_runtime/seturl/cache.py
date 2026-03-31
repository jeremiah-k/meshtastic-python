"""Local cache updates, invalidation, and restoration for setURL transactions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from meshtastic.node_runtime.seturl.helpers import _channels_fingerprint
from meshtastic.protobuf import channel_pb2, config_pb2

if TYPE_CHECKING:
    from meshtastic.node import Node

logger = logging.getLogger(__name__)

_ERR_CONFIG_OR_CHANNELS_NOT_LOADED = "Config or channels not loaded"


class _SetUrlCacheManager:
    """Owns local cache updates, invalidation, and restoration for setURL transactions."""

    def __init__(self, node: Node) -> None:
        self._node = node

    def _invalidate_channel_cache_locked(self, warning_message: str) -> None:
        """Invalidate channel cache while already holding ``_channels_lock``."""
        self._node.channels = None
        self._node.partialChannels = []
        logger.warning("%s", warning_message)

    def apply_add_only_success(
        self,
        channels_to_write: list[tuple[channel_pb2.Channel, str]],
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
    ) -> None:
        """Apply addOnly channel cache updates after remote writes succeed."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node.partialChannels = []
                logger.warning(
                    "Channel cache unavailable after successful addOnly apply; reload channels to refresh local state."
                )
                return
            if (
                expected_channels_fingerprint is not None
                and _channels_fingerprint(channels) != expected_channels_fingerprint
            ):
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during addOnly cache update; invalidating local channel cache."
                )
                return
            for staged_channel, _ in channels_to_write:
                if 0 <= staged_channel.index < len(channels):
                    channels[staged_channel.index].CopyFrom(staged_channel)
                else:
                    self._invalidate_channel_cache_locked(
                        f"Channel index {staged_channel.index} out of range during addOnly cache update; invalidating local channel cache."
                    )
                    break
            else:
                self._node.partialChannels = []

    def apply_replace_channel_write(
        self,
        staged_channel: channel_pb2.Channel,
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
        expected_channels_ref: list[channel_pb2.Channel] | None = None,
    ) -> None:
        """Apply one replace-path channel cache update after a successful write."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            if channels is None:
                self._node._raise_interface_error(  # noqa: SLF001
                    _ERR_CONFIG_OR_CHANNELS_NOT_LOADED
                )
                return  # unreachable; satisfies type checker
            channels_changed = False
            if (
                expected_channels_ref is not None
                and channels is not expected_channels_ref
            ):
                channels_changed = True
            if not channels_changed and expected_channels_fingerprint is not None:
                channels_changed = (
                    _channels_fingerprint(channels) != expected_channels_fingerprint
                )
            if channels_changed:
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during replace-all cache update; invalidating local channel cache."
                )
                self._node._raise_interface_error(  # noqa: SLF001
                    "Channel cache changed during replace-all cache update; aborting transaction."
                )
                return  # unreachable; satisfies type checker
            if not 0 <= staged_channel.index < len(channels):
                self._node._raise_interface_error(  # noqa: SLF001
                    f"Channel index {staged_channel.index} out of range during cache update"
                )
                return  # unreachable; satisfies type checker
            channels[staged_channel.index].CopyFrom(staged_channel)
            self._node.partialChannels = []

    def invalidate_channel_cache(self, warning_message: str) -> None:
        """Invalidate local channel cache after incomplete rollback."""
        with self._node._channels_lock:  # noqa: SLF001
            self._invalidate_channel_cache_locked(warning_message)

    def restore_replace_channels_snapshot(
        self,
        replace_original_channels_snapshot: list[channel_pb2.Channel],
        *,
        expected_channels_fingerprint: tuple[bytes, ...] | None = None,
        expected_channels_ref: list[channel_pb2.Channel] | None = None,
    ) -> None:
        """Restore replace-all channel cache from pre-transaction snapshot."""
        with self._node._channels_lock:  # noqa: SLF001
            channels = self._node.channels
            channels_changed = False
            if (
                expected_channels_ref is not None
                and channels is not None
                and channels is not expected_channels_ref
            ):
                channels_changed = True
            if (
                not channels_changed
                and expected_channels_fingerprint is not None
                and channels is not None
            ):
                channels_changed = (
                    _channels_fingerprint(channels) != expected_channels_fingerprint
                )
            if channels_changed:
                self._invalidate_channel_cache_locked(
                    "Channel cache changed during replace-all rollback restore; invalidating local channel cache."
                )
                return
            restored_channels: list[channel_pb2.Channel] = []
            for original_channel in replace_original_channels_snapshot:
                restored_channel = channel_pb2.Channel()
                restored_channel.CopyFrom(original_channel)
                restored_channels.append(restored_channel)
            self._node.channels = restored_channels
            self._node.partialChannels = []

    def apply_lora_success(self, lora_config: config_pb2.Config.LoRaConfig) -> None:
        """Apply successful LoRa cache update."""

        def _apply() -> None:
            self._node.localConfig.lora.CopyFrom(lora_config)

        self._node._execute_with_node_db_lock(_apply)

    def restore_lora_snapshot(
        self,
        original_lora_config: config_pb2.Config.LoRaConfig,
    ) -> None:
        """Restore LoRa cache from pre-transaction snapshot."""

        def _restore() -> None:
            self._node.localConfig.lora.CopyFrom(original_lora_config)

        self._node._execute_with_node_db_lock(_restore)

    def clear_lora_cache_with_warning(self, warning_message: str) -> None:
        """Clear LoRa cache when rollback cannot restore prior value."""

        def _clear() -> None:
            self._node.localConfig.ClearField("lora")

        self._node._execute_with_node_db_lock(_clear)
        logger.warning("%s", warning_message)
