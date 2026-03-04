"""Targeted tests for Windows COM-port detection helpers in util.py."""

from unittest.mock import MagicMock, patch

import pytest

from meshtastic.supported_device import SupportedDevice
from meshtastic.util import detect_windows_port, detectWindowsPort


@pytest.mark.unit
@patch("platform.system", return_value="Windows")
@patch(
    "subprocess.getstatusoutput",
    return_value=(0, "DeviceId : USB\\\\VID_303A&PID_1001 (COM12)"),
)
def test_detectWindowsPort_parses_com_port_from_powershell_output(
    _mock_subprocess: MagicMock,
    _mock_system: MagicMock,
) -> None:
    """detectWindowsPort should parse COM ports from PowerShell output on Windows."""
    device = SupportedDevice(
        name="x",
        for_firmware="heltec-v3",
        usb_vendor_id_in_hex="303A",
        usb_product_id_in_hex="1001",
    )

    assert detectWindowsPort(device) == {"COM12"}


@pytest.mark.unit
@patch("meshtastic.util.detectWindowsPort", return_value={"COM7"})
def test_detect_windows_port_alias_delegates(
    wrapped: MagicMock,
) -> None:
    """detect_windows_port should delegate to detectWindowsPort."""
    device = SupportedDevice(name="x", for_firmware="heltec-v3")

    assert detect_windows_port(device) == {"COM7"}
    wrapped.assert_called_once_with(device)
