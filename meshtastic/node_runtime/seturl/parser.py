"""URL fragment decoding and ChannelSet parse/validation."""

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

from google.protobuf.message import DecodeError

from meshtastic.protobuf import apponly_pb2


@dataclass(frozen=True)
class _SetUrlParsedInput:
    """Parsed/decoded setURL input.

    Attributes
    ----------
    channel_set : apponly_pb2.ChannelSet
        The decoded channel set from the URL fragment.
    has_lora_update : bool
        Whether the channel set contains a LoRa configuration update.
    """

    channel_set: apponly_pb2.ChannelSet
    has_lora_update: bool


class _SetUrlParser:
    """Owns URL fragment decoding and ChannelSet parse/validation."""

    @staticmethod
    def _parse(
        url: str,
        *,
        raise_interface_error: Callable[[str], NoReturn],
    ) -> _SetUrlParsedInput:
        """Parse URL fragment into a ChannelSet payload with setURL validations.

        Parameters
        ----------
        url : str
            The URL containing the base64-encoded channel set data.
        raise_interface_error : Callable[[str], NoReturn]
            Callback function to raise an interface error with a message.

        Returns
        -------
        _SetUrlParsedInput
            The parsed channel set and LoRa update flag.
        """
        # URLs are of the form https://meshtastic.org/d/#{base64_channel_set}
        # Parse from '#' to support optional query parameters before the fragment.
        if "#" not in url:
            raise_interface_error("Invalid URL")
        b64 = url.split("#")[-1]
        if not b64:
            raise_interface_error("Invalid URL: no channel data found")

        # We normally strip padding to make for a shorter URL, but the python parser doesn't like
        # that.  So add back any missing padding
        # per https://stackoverflow.com/a/9807138
        missing_padding = len(b64) % 4
        if missing_padding:
            b64 += "=" * (4 - missing_padding)

        try:
            decoded_url = base64.b64decode(b64, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as ex:
            raise_interface_error(f"Invalid URL: {ex}")

        channel_set = apponly_pb2.ChannelSet()
        try:
            channel_set.ParseFromString(decoded_url)
        except (DecodeError, ValueError) as ex:
            raise_interface_error(f"Unable to parse channel settings from URL: {ex}")

        if not channel_set.settings:
            raise_interface_error("There were no settings.")
        return _SetUrlParsedInput(
            channel_set=channel_set,
            has_lora_update=channel_set.HasField("lora_config"),
        )
