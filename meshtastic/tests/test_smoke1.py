"""Meshtastic smoke tests with a single device via USB."""

import os
import platform
import re
import shlex
import time
from pathlib import Path

# Do not like using hard coded sleeps, but it probably makes
# sense to pause for the radio at appropriate times
import pytest

from ..util import findPorts
from .cli_test_utils import run_cli_with_timeout

# seconds to pause after running a meshtastic command
PAUSE_AFTER_COMMAND = 2
PAUSE_AFTER_REBOOT = 7


def _quote_shell_path(path: str) -> str:
    """Quote a filesystem path for shell command usage in run_cli_with_timeout."""
    if os.name == "nt":
        escaped = path.replace('"', '""')
        return f'"{escaped}"'
    return shlex.quote(path)


@pytest.mark.smoke1
def test_smoke1_reboot() -> None:
    """Test reboot."""
    return_value, _ = run_cli_with_timeout("meshtastic --reboot")
    assert return_value == 0
    # pause for the radio to reset (10 seconds for the pause, and a few more seconds to be back up)
    time.sleep(18)


@pytest.mark.smoke1
def test_smoke1_info() -> None:
    """Test --info."""
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Owner", out, re.MULTILINE)
    assert re.search(r"^My info", out, re.MULTILINE)
    assert re.search(r"^Nodes in mesh", out, re.MULTILINE)
    assert re.search(r"^Preferences", out, re.MULTILINE)
    assert re.search(r"^Channels", out, re.MULTILINE)
    assert re.search(r"^  PRIMARY", out, re.MULTILINE)
    assert re.search(r"^Primary channel URL", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_get_with_invalid_setting() -> None:
    """Test '--get a_bad_setting'."""
    return_value, out = run_cli_with_timeout("meshtastic --get a_bad_setting")
    assert re.search(r"Choices in sorted order", out)
    assert return_value == 0


@pytest.mark.smoke1
def test_set_with_invalid_setting() -> None:
    """Test '--set a_bad_setting'."""
    return_value, out = run_cli_with_timeout("meshtastic --set a_bad_setting foo")
    assert re.search(r"Choices in sorted order", out)
    assert return_value == 0


@pytest.mark.smoke1
def test_ch_set_with_invalid_setting() -> None:
    """Test '--ch-set with a_bad_setting'."""
    return_value, out = run_cli_with_timeout(
        "meshtastic --ch-set invalid_setting foo --ch-index 0"
    )
    assert re.search(r"Choices in sorted order", out)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_pos_fields() -> None:
    """Test --pos-fields (with some values POS_ALTITUDE POS_ALT_MSL POS_BATTERY)."""
    return_value, out = run_cli_with_timeout(
        "meshtastic --pos-fields POS_ALTITUDE POS_ALT_MSL POS_BATTERY"
    )
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Setting position fields to 35", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --pos-fields")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"POS_ALTITUDE", out, re.MULTILINE)
    assert re.search(r"POS_ALT_MSL", out, re.MULTILINE)
    assert re.search(r"POS_BATTERY", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_test_with_arg_but_no_hardware() -> None:
    """Test --test with one connected device (expects warning path)."""
    return_value, out = run_cli_with_timeout("meshtastic --test")
    assert re.search(r"^Warning: Must have at least two devices", out, re.MULTILINE)
    assert return_value == 1


@pytest.mark.smoke1
def test_smoke1_debug() -> None:
    """Test --debug."""
    return_value, out = run_cli_with_timeout("meshtastic --info --debug")
    assert re.search(r"^Owner", out, re.MULTILINE)
    assert re.search(r"^DEBUG file", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_seriallog_to_file() -> None:
    """Test --seriallog to a file creates a file."""
    filename = "tmpoutput.txt"
    if os.path.exists(filename):
        os.remove(filename)
    return_value, _ = run_cli_with_timeout(f"meshtastic --info --seriallog {filename}")
    assert os.path.exists(filename)
    assert return_value == 0
    os.remove(filename)


@pytest.mark.smoke1
def test_smoke1_qr() -> None:
    """Test --qr."""
    filename = "tmpqr"
    if os.path.exists(f"{filename}"):
        os.remove(f"{filename}")
    return_value, _ = run_cli_with_timeout(f"meshtastic --qr > {filename}")
    assert os.path.exists(f"{filename}")
    # not really testing that a valid qr code is created, just that the file size
    # is reasonably big enough for a qr code
    assert os.stat(f"{filename}").st_size > 20000
    assert return_value == 0
    os.remove(f"{filename}")


@pytest.mark.smoke1
def test_smoke1_nodes() -> None:
    """Test --nodes."""
    return_value, out = run_cli_with_timeout("meshtastic --nodes")
    assert re.match(r"Connected to radio", out)
    if platform.system() != "Windows":
        assert re.search(r" User ", out, re.MULTILINE)
        assert re.search(r"  1 ", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_send_hello() -> None:
    """Test --sendtext hello."""
    return_value, out = run_cli_with_timeout("meshtastic --sendtext hello")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Sending text message hello to \^all", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_port() -> None:
    """Test --port."""
    # first, get the ports
    ports = findPorts(True)
    # hopefully there is just one
    assert len(ports) == 1
    port = ports[0]
    return_value, out = run_cli_with_timeout(f"meshtastic --port {port} --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Owner", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_set_location_info() -> None:
    """Test --setlat, --setlon and --setalt."""
    return_value, out = run_cli_with_timeout(
        "meshtastic --setlat 32.7767 --setlon -96.7970 --setalt 1337"
    )
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Fixing altitude", out, re.MULTILINE)
    assert re.search(r"^Fixing latitude", out, re.MULTILINE)
    assert re.search(r"^Fixing longitude", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out2 = run_cli_with_timeout("meshtastic --info")
    assert re.search(r"1337", out2, re.MULTILINE)
    assert re.search(r"32.7767", out2, re.MULTILINE)
    assert re.search(r"-96.797", out2, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_set_owner() -> None:
    """Test --set-owner name."""
    # make sure the owner is not Joe
    return_value, out = run_cli_with_timeout("meshtastic --set-owner Bob")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Setting device owner to Bob", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert not re.search(r"Owner: Joe", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --set-owner Joe")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Setting device owner to Joe", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.search(r"Owner: Joe", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_ch_set_modem_config() -> None:
    """Test --ch-set modem_config."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-set modem_config MedFast")
    assert re.search(r"Warning: Need to specify", out, re.MULTILINE)
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert not re.search(r"MedFast", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout(
        "meshtastic --ch-set modem_config MedFast --ch-index 0"
    )
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Set modem_config to MedFast", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_REBOOT)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.search(r"MedFast", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_ch_values() -> None:
    """Test --ch-vlongslow --ch-longslow, --ch-longfast, --ch-medslow, --ch-medfast,
    --ch-shortslow, and --ch-shortfast arguments.
    """
    exp = {
        "--ch-vlongslow": '{ "psk": "AQ==" }',
        "--ch-longslow": "LongSlow",
        "--ch-longfast": "LongFast",
        "--ch-medslow": "MedSlow",
        "--ch-medfast": "MedFast",
        "--ch-shortslow": "ShortSlow",
        "--ch-shortfast": "ShortFast",
    }

    for key, val in exp.items():
        return_value, out = run_cli_with_timeout(f"meshtastic {key}")
        assert re.match(r"Connected to radio", out)
        assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
        assert return_value == 0
        # pause for the radio (might reboot)
        time.sleep(PAUSE_AFTER_REBOOT)
        return_value, out = run_cli_with_timeout("meshtastic --info")
        assert re.search(val, out, re.MULTILINE)
        assert return_value == 0
        # pause for the radio
        time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_ch_set_name() -> None:
    """Test --ch-set name."""
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert not re.search(r"MyChannel", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-set name MyChannel")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"Warning: Need to specify", out, re.MULTILINE)
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout(
        "meshtastic --ch-set name MyChannel --ch-index 0"
    )
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Set name to MyChannel", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.search(r"MyChannel", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_ch_set_downlink_and_uplink() -> None:
    """Test --ch-set downlink_enabled X and --ch-set uplink_enabled X."""
    return_value, out = run_cli_with_timeout(
        "meshtastic --ch-set downlink_enabled false --ch-set uplink_enabled false"
    )
    assert re.match(r"Connected to radio", out)
    assert re.search(r"Warning: Need to specify", out, re.MULTILINE)
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout(
        "meshtastic --ch-set downlink_enabled false --ch-set uplink_enabled false --ch-index 0"
    )
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert not re.search(r"uplinkEnabled", out, re.MULTILINE)
    assert not re.search(r"downlinkEnabled", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout(
        "meshtastic --ch-set downlink_enabled true --ch-set uplink_enabled true --ch-index 0"
    )
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Set downlink_enabled to true", out, re.MULTILINE)
    assert re.search(r"^Set uplink_enabled to true", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.search(r"uplinkEnabled", out, re.MULTILINE)
    assert re.search(r"downlinkEnabled", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_ch_add_and_ch_del() -> None:
    """Test --ch-add and --ch-del."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-add testing")
    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"SECONDARY", out, re.MULTILINE)
    assert re.search(r"testing", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-index 1 --ch-del")
    assert re.search(r"Deleting channel 1", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_REBOOT)
    # make sure the secondary channel is not there
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert not re.search(r"SECONDARY", out, re.MULTILINE)
    assert not re.search(r"testing", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_ch_enable_and_disable() -> None:
    """Test --ch-enable and --ch-disable."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-add testing")
    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"SECONDARY", out, re.MULTILINE)
    assert re.search(r"testing", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    # ensure they need to specify a --ch-index
    return_value, out = run_cli_with_timeout("meshtastic --ch-disable")
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-disable --ch-index 1")
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"DISABLED", out, re.MULTILINE)
    assert re.search(r"testing", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-enable --ch-index 1")
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"SECONDARY", out, re.MULTILINE)
    assert re.search(r"testing", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-del --ch-index 1")
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_ch_del_a_disabled_non_primary_channel() -> None:
    """Test --ch-del will work on a disabled non-primary channel."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-add testing")
    assert re.search(r"Writing modified channels to device", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"SECONDARY", out, re.MULTILINE)
    assert re.search(r"testing", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    # ensure they need to specify a --ch-index
    return_value, out = run_cli_with_timeout("meshtastic --ch-disable")
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-del --ch-index 1")
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert not re.search(r"DISABLED", out, re.MULTILINE)
    assert not re.search(r"SECONDARY", out, re.MULTILINE)
    assert not re.search(r"testing", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_attempt_to_delete_primary_channel() -> None:
    """Test that we cannot delete the PRIMARY channel."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-del --ch-index 0")
    assert re.search(r"Warning: Cannot delete primary channel", out, re.MULTILINE)
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_attempt_to_disable_primary_channel() -> None:
    """Test that we cannot disable the PRIMARY channel."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-disable --ch-index 0")
    assert re.search(r"Warning: Cannot enable", out, re.MULTILINE)
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_attempt_to_enable_primary_channel() -> None:
    """Test that we cannot enable the PRIMARY channel."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-enable --ch-index 0")
    assert re.search(r"Warning: Cannot enable", out, re.MULTILINE)
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_ensure_ch_del_second_of_three_channels() -> None:
    """Test that when we delete the 2nd of 3 channels, that it deletes the correct channel."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-add testing1")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"SECONDARY", out, re.MULTILINE)
    assert re.search(r"testing1", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-add testing2")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"testing2", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-del --ch-index 1")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"testing2", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-del --ch-index 1")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_ensure_ch_del_third_of_three_channels() -> None:
    """Test that when we delete the 3rd of 3 channels, that it deletes the correct channel."""
    return_value, out = run_cli_with_timeout("meshtastic --ch-add testing1")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"SECONDARY", out, re.MULTILINE)
    assert re.search(r"testing1", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-add testing2")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"testing2", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-del --ch-index 2")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"testing1", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --ch-del --ch-index 1")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_seturl_default() -> None:
    """Test --seturl with default value."""
    # set some channel value so we no longer have a default channel
    return_value, out = run_cli_with_timeout(
        "meshtastic --ch-set name foo --ch-index 0"
    )
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    # ensure we no longer have a default primary channel
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert not re.search("CgUYAyIBAQ", out, re.MULTILINE)
    assert return_value == 0
    url = "https://www.meshtastic.org/d/#CgUYAyIBAQ"
    return_value, out = run_cli_with_timeout(f"meshtastic --seturl {url}")
    assert re.match(r"Connected to radio", out)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.search("CgUYAyIBAQ", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_seturl_invalid_url() -> None:
    """Test --seturl with invalid url."""
    # Note: This url is no longer a valid url.
    url = "https://www.meshtastic.org/c/#GAMiENTxuzogKQdZ8Lz_q89Oab8qB0RlZmF1bHQ="
    return_value, out = run_cli_with_timeout(f"meshtastic --seturl {url}")
    assert re.match(r"Connected to radio", out)
    assert re.search("Warning: There were no settings", out, re.MULTILINE)
    assert return_value == 1
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)


@pytest.mark.smoke1
def test_smoke1_configure() -> None:
    """Test --configure."""
    config_path = Path(__file__).resolve().parents[2] / "example_config.yaml"
    assert config_path.exists(), f"Config file not found: {config_path}"
    return_value, out = run_cli_with_timeout(
        f"meshtastic --configure {_quote_shell_path(str(config_path))}"
    )
    assert re.match(r"Connected to radio", out)
    assert re.search("^Setting device owner to Bob TBeam", out, re.MULTILINE)
    assert re.search("^Fixing altitude at 304 meters", out, re.MULTILINE)
    assert re.search("^Fixing latitude at 35.8", out, re.MULTILINE)
    assert re.search("^Fixing longitude at -93.8", out, re.MULTILINE)
    assert re.search("^Setting device position", out, re.MULTILINE)
    assert re.search("^Set region to 1", out, re.MULTILINE)
    assert re.search("^Set is_always_powered to true", out, re.MULTILINE)
    assert re.search("^Set screen_on_secs to 31536000", out, re.MULTILINE)
    assert re.search("^Set wait_bluetooth_secs to 31536000", out, re.MULTILINE)
    assert re.search("^Writing modified preferences to device", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_REBOOT)


@pytest.mark.smoke1
def test_smoke1_set_ham() -> None:
    """Test --set-ham (followed by factory reset in later test)."""
    return_value, out = run_cli_with_timeout("meshtastic --set-ham KI1234")
    assert re.search(r"Setting Ham ID", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_REBOOT)
    return_value, out = run_cli_with_timeout("meshtastic --info")
    assert re.search(r"Owner: KI1234", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_set_wifi_settings() -> None:
    """Test --set wifi_ssid and --set wifi_password."""
    return_value, out = run_cli_with_timeout(
        'meshtastic --set wifi_ssid "some_ssid" --set wifi_password "temp1234"'
    )
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Set wifi_ssid to some_ssid", out, re.MULTILINE)
    assert re.search(r"^Set wifi_password to temp1234", out, re.MULTILINE)
    assert return_value == 0
    # pause for the radio
    time.sleep(PAUSE_AFTER_COMMAND)
    return_value, out = run_cli_with_timeout(
        "meshtastic --get wifi_ssid --get wifi_password"
    )
    assert re.search(r"^wifi_ssid: some_ssid", out, re.MULTILINE)
    assert re.search(r"^wifi_password: sekrit", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_factory_reset() -> None:
    """Test factory reset."""
    return_value, out = run_cli_with_timeout("meshtastic --set factory_reset true")
    assert re.match(r"Connected to radio", out)
    assert re.search(r"^Set factory_reset to true", out, re.MULTILINE)
    assert re.search(r"^Writing modified preferences to device", out, re.MULTILINE)
    assert return_value == 0
    # NOTE: The radio may not be responsive after this, may need to do a manual reboot
    # by pressing the button
