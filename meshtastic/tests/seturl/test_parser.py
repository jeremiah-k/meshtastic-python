"""Tests for _SetUrlParser."""

import base64
from collections.abc import Callable
from typing import NoReturn

import pytest

from meshtastic.node_runtime.seturl_runtime import (
    _SetUrlParsedInput,
    _SetUrlParser,
)
from meshtastic.protobuf import apponly_pb2
from meshtastic.tests.seturl.conftest import (
    _make_channel_set_with_lora,
    _make_valid_channel_set_url,
)


def _raise_error_for_valid_parse(msg: str) -> NoReturn:
    """Fail test if called during successful parse cases."""
    pytest.fail(f"Unexpected parse error: {msg}")


def _make_capturing_raise_error(captured_msg: list[str]) -> Callable[[str], NoReturn]:
    """Create a raise_error callback that captures the message and raises ValueError."""

    def raise_error(msg: str) -> NoReturn:
        captured_msg.append(msg)
        raise ValueError(msg)

    return raise_error


class TestSetUrlParser:
    """Tests for _SetUrlParser."""

    @pytest.mark.unit
    def test_parse_with_valid_url_returns_parsed_input(self) -> None:
        """parse() with valid URL returns parsed config."""
        url = _make_valid_channel_set_url("testchannel")

        result = _SetUrlParser._parse(
            url, raise_interface_error=_raise_error_for_valid_parse
        )

        assert isinstance(result, _SetUrlParsedInput)
        assert len(result.channel_set.settings) == 1
        assert result.channel_set.settings[0].name == "testchannel"
        assert result.has_lora_update is False

    @pytest.mark.unit
    def test_parse_with_lora_config_sets_flag(self) -> None:
        """parse() with URL containing LoRa config sets has_lora_update."""
        channel_set = _make_channel_set_with_lora("test")

        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode(
            "utf-8"
        )
        url = f"https://meshtastic.org/d/#{encoded}"

        result = _SetUrlParser._parse(
            url, raise_interface_error=_raise_error_for_valid_parse
        )

        assert result.has_lora_update is True
        assert result.channel_set.lora_config.hop_limit == 3

    @pytest.mark.unit
    def test_parse_without_hash_raises_error(self) -> None:
        """parse() with URL missing hash fragment raises error."""
        url = "https://meshtastic.org/d/"

        captured_msg: list[str] = []

        with pytest.raises(ValueError, match="Invalid URL"):
            _SetUrlParser._parse(
                url, raise_interface_error=_make_capturing_raise_error(captured_msg)
            )

        assert len(captured_msg) == 1
        assert captured_msg[0] == "Invalid URL"

    @pytest.mark.unit
    def test_parse_with_empty_hash_raises_error(self) -> None:
        """parse() with empty hash fragment raises error."""
        url = "https://meshtastic.org/d/#"

        captured_msg: list[str] = []

        with pytest.raises(ValueError, match="Invalid URL"):
            _SetUrlParser._parse(
                url, raise_interface_error=_make_capturing_raise_error(captured_msg)
            )

        assert len(captured_msg) == 1
        assert "no channel data" in captured_msg[0]

    @pytest.mark.unit
    def test_parse_with_invalid_base64_raises_error(self) -> None:
        """parse() with invalid base64 raises error."""
        url = "https://meshtastic.org/d/#!!!invalid-base64!!!"

        captured_msg: list[str] = []

        with pytest.raises(ValueError, match="Invalid URL"):
            _SetUrlParser._parse(
                url, raise_interface_error=_make_capturing_raise_error(captured_msg)
            )

        assert len(captured_msg) == 1
        assert "Invalid URL" in captured_msg[0]

    @pytest.mark.unit
    def test_parse_with_no_settings_raises_error(self) -> None:
        """parse() with empty ChannelSet raises error."""
        channel_set = apponly_pb2.ChannelSet()
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode(
            "utf-8"
        )
        url = f"https://meshtastic.org/d/#{encoded}"

        captured_msg: list[str] = []

        with pytest.raises(ValueError, match="Invalid URL"):
            _SetUrlParser._parse(
                url, raise_interface_error=_make_capturing_raise_error(captured_msg)
            )

        assert len(captured_msg) == 1
        # Empty ChannelSet serializes to empty bytes, so we hit the "no channel data" check
        assert "no channel data found" in captured_msg[0]

    @pytest.mark.unit
    def test_parse_adds_padding_to_base64(self) -> None:
        """parse() correctly adds padding to unpadded base64."""
        channel_set = apponly_pb2.ChannelSet()
        settings = channel_set.settings.add()
        settings.name = "test"
        settings.psk = b"\x01"
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode(
            "utf-8"
        )
        encoded_unpadded = encoded.rstrip("=")
        url = f"https://meshtastic.org/d/#{encoded_unpadded}"

        result = _SetUrlParser._parse(
            url, raise_interface_error=_raise_error_for_valid_parse
        )

        assert result.channel_set.settings[0].name == "test"
