"""Meshtastic smoke tests with one device connected via USB serial.

This module intentionally splits coverage into two lanes:
- `smoke1`: stable/non-destructive checks suitable for routine hardware runs.
- `smoke1_destructive`: reboot/reset and heavy config mutation checks that are
  opt-in and may temporarily leave hardware in a modified state.
"""

import contextlib
import platform
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from ..util import findPorts
from .cli_test_utils import (
    _quote_shell_path,
    run_cli_argv_with_timeout,
    run_cli_with_timeout,
)

PAUSE_AFTER_COMMAND = 2
PAUSE_AFTER_REBOOT = 7
INFO_READY_TIMEOUT_SECONDS = 60
INFO_READY_POLL_INTERVAL_SECONDS = 2
RESTORE_ATTEMPTS = 8
RESTORE_RETRY_DELAY_SECONDS = 10
DEFAULT_URL_FRAGMENT = "CgUYAyIBAQ"
CHANNEL_PRESET_INFO_PATTERNS: dict[str, str] = {
    "--ch-vlongslow": "VeryLongSlow",
    "--ch-longslow": "LongSlow",
    "--ch-longfast": "LongFast",
    "--ch-medslow": "MedSlow",
    "--ch-medfast": "MedFast",
    "--ch-shortslow": "ShortSlow",
    "--ch-shortfast": "ShortFast",
}


def _run(*argv: str, timeout: int | float = 120) -> tuple[int, str]:
    """Run a smoke command via argv and return `(return_code, output)`."""
    result = run_cli_argv_with_timeout(list(argv), timeout=timeout)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _run_shell(command: str, timeout: int | float = 120) -> tuple[int, str]:
    """Run a shell command for tests that require shell syntax like redirection."""
    return run_cli_with_timeout(command, timeout=timeout)


def _destructive_test(func: Callable[..., object]) -> Callable[..., object]:
    """Mark a smoke1 test as destructive and restore baseline config after it."""
    return cast(
        Callable[..., object],
        pytest.mark.usefixtures("restore_smoke1_module_config")(
            pytest.mark.smoke1(pytest.mark.smoke1_destructive(func))
        ),
    )


def _assert_connected(output: str) -> None:
    """Assert that CLI output indicates serial connection succeeded."""
    assert re.search(r"^Connected to radio", output, re.MULTILINE)


def _unique_channel_name(prefix: str = "t") -> str:
    """Create a short unique channel name within firmware length limits."""
    return f"{prefix}{uuid.uuid4().hex[:6]}"


def _extract_added_channel_index(output: str) -> int | None:
    """Parse `--ch-add` output for the suggested channel index."""
    match = re.search(r"newly-added channel's (\d+) as '--ch-index'", output)
    if match is None:
        return None
    return int(match.group(1))


def _find_channel_index_by_name(info_output: str, channel_name: str) -> int | None:
    """Find the first channel index whose serialized channel name matches."""
    section_pattern = re.compile(
        r"(?ms)^\s*Index\s+(\d+):\s+\w+.*?(?=^\s*Index\s+\d+:|\Z)"
    )
    for match in section_pattern.finditer(info_output):
        if re.search(rf'"name":\s+"{re.escape(channel_name)}"', match.group(0)):
            return int(match.group(1))
    return None


@pytest.mark.unit
def test_find_channel_index_by_name_handles_multiline_channel_blocks() -> None:
    """Channel lookup should work when channel JSON spans multiple lines."""
    info_output = """
    Index 0: PRIMARY
      {
        "name": "alpha"
      }
    Index 2: SECONDARY
      {
        "role": "SECONDARY",
        "name": "demo"
      }
    """
    assert _find_channel_index_by_name(info_output, "demo") == 2
    assert _find_channel_index_by_name(info_output, "alpha") == 0
    assert _find_channel_index_by_name(info_output, "nonexistent") is None


def _restore_config_with_retries(config_path: Path) -> tuple[int, str]:
    """Attempt to restore a saved config with retries to handle reboot windows."""
    output = ""
    result = 1
    for attempt in range(RESTORE_ATTEMPTS):
        result, output = _run(
            "meshtastic",
            "--configure",
            str(config_path),
            timeout=180,
        )
        if result == 0:
            return result, output
        if attempt + 1 < RESTORE_ATTEMPTS:
            time.sleep(RESTORE_RETRY_DELAY_SECONDS)
    return result, output


def _wait_for_info_ready(
    *,
    timeout: int | float = INFO_READY_TIMEOUT_SECONDS,
    poll_interval: int | float = INFO_READY_POLL_INTERVAL_SECONDS,
) -> tuple[int, str]:
    """Poll `meshtastic --info` until the device responds or the timeout expires."""
    deadline = time.monotonic() + timeout
    last_result = 1, ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last_result
        probe_timeout = min(max(1.0, poll_interval), remaining)
        last_result = _run("meshtastic", "--info", timeout=probe_timeout)
        code, output = last_result
        if code == 0:
            return code, output
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last_result
        time.sleep(min(poll_interval, remaining))


@pytest.fixture
def restore_smoke1_module_config() -> Iterator[None]:
    """Export baseline config before a destructive test and restore it afterward.

    Destructive hardware tests must not leak config mutations into later tests,
    so each opted-in test gets its own export/restore cycle.
    """
    export_succeeded = False
    restore_succeeded = False
    with tempfile.NamedTemporaryFile(
        prefix="meshtastic-smoke1-module-backup-",
        suffix=".yaml",
        delete=False,
    ) as temp_file:
        backup_path = Path(temp_file.name)
    try:
        return_value, out = _run(
            "meshtastic",
            "--export-config",
            str(backup_path),
            timeout=180,
        )
        if return_value != 0:
            pytest.skip(f"Unable to export baseline smoke1 config:\n{out}")
        export_succeeded = True

        yield

        restore_code, restore_output = _restore_config_with_retries(backup_path)
        assert restore_code == 0, (
            "Failed to restore smoke1 baseline configuration:\n"
            f"{restore_output}\n"
            f"backup={backup_path}"
        )
        ready_code, ready_output = _wait_for_info_ready()
        assert ready_code == 0, (
            "Smoke1 device did not become ready after baseline restore:\n"
            f"{ready_output}\n"
            f"backup={backup_path}"
        )
        _assert_connected(ready_output)
        restore_succeeded = True
    finally:
        if not export_succeeded or restore_succeeded:
            with contextlib.suppress(FileNotFoundError):
                backup_path.unlink()


@pytest.mark.smoke1
def test_smoke1_info() -> None:
    """`--info` should print core sections and at least one PRIMARY channel."""
    return_value, out = _run("meshtastic", "--info")
    _assert_connected(out)
    assert re.search(r"^Owner", out, re.MULTILINE)
    assert re.search(r"^My info", out, re.MULTILINE)
    assert re.search(r"^Nodes in mesh", out, re.MULTILINE)
    assert re.search(r"^Preferences", out, re.MULTILINE)
    assert re.search(r"^Channels", out, re.MULTILINE)
    assert re.search(r"^\s*Index\s+\d+:\s+PRIMARY", out, re.MULTILINE)
    assert re.search(r"^Primary channel URL", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_get_with_invalid_setting() -> None:
    """Invalid `--get` should list available fields without crashing."""
    return_value, out = _run("meshtastic", "--get", "a_bad_setting")
    _assert_connected(out)
    assert "do not have an attribute a_bad_setting" in out
    assert "Choices are..." in out
    assert return_value == 0


@pytest.mark.smoke1
def test_set_with_invalid_setting() -> None:
    """Invalid `--set` should list available fields without crashing."""
    return_value, out = _run("meshtastic", "--set", "a_bad_setting", "foo")
    _assert_connected(out)
    assert "do not have an attribute a_bad_setting" in out
    assert "Choices are..." in out
    assert return_value == 0


@pytest.mark.smoke1
def test_ch_set_with_invalid_setting() -> None:
    """Invalid `--ch-set` should list channel field choices."""
    return_value, out = _run(
        "meshtastic",
        "--ch-set",
        "invalid_setting",
        "foo",
        "--ch-index",
        "0",
    )
    _assert_connected(out)
    assert "does not have an attribute invalid_setting" in out
    assert "Choices are..." in out
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_test_with_arg_but_no_hardware() -> None:
    """`--test` should fail cleanly when only one serial device is present."""
    return_value, out = _run("meshtastic", "--test")
    assert "Must have at least two devices connected to USB" in out
    assert "Warning: Test was not successful." in out
    assert return_value == 1


@pytest.mark.smoke1
def test_smoke1_debug() -> None:
    """`--debug` should emit debug file location and still return success."""
    return_value, out = _run("meshtastic", "--info", "--debug")
    assert re.search(r"^Owner", out, re.MULTILINE)
    assert re.search(r"^DEBUG file", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_seriallog_to_file() -> None:
    """`--seriallog` should create a log file."""
    filepath = Path("tmpoutput.txt")
    try:
        if filepath.exists():
            filepath.unlink()
        return_value, _ = _run("meshtastic", "--info", "--seriallog", str(filepath))
        assert filepath.exists()
        assert return_value == 0
    finally:
        if filepath.exists():
            filepath.unlink()


@pytest.mark.smoke1
def test_smoke1_qr() -> None:
    """`--qr` should emit a PNG-sized payload to stdout redirection."""
    filename = Path("tmpqr")
    try:
        if filename.exists():
            filename.unlink()
        return_value, _ = _run_shell(f"meshtastic --qr > {_quote_shell_path(filename)}")
        assert filename.exists()
        assert filename.stat().st_size > 20000
        assert return_value == 0
    finally:
        if filename.exists():
            filename.unlink()


@pytest.mark.smoke1
def test_smoke1_nodes() -> None:
    """`--nodes` should render a connected node table on non-Windows."""
    return_value, out = _run("meshtastic", "--nodes")
    _assert_connected(out)
    if platform.system() != "Windows":
        assert re.search(r" User ", out, re.MULTILINE)
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_send_hello() -> None:
    """`--sendtext` should enqueue a broadcast text packet."""
    return_value, out = _run("meshtastic", "--sendtext", "hello")
    _assert_connected(out)
    assert "Sending text message hello" in out
    assert return_value == 0


@pytest.mark.smoke1
def test_smoke1_port() -> None:
    """`--port <detected-port> --info` should connect to the same radio."""
    ports = findPorts(eliminate_duplicates=True)
    assert len(ports) == 1
    port = ports[0]
    return_value, out = _run("meshtastic", "--port", port, "--info")
    _assert_connected(out)
    assert re.search(r"^Owner", out, re.MULTILINE)
    assert return_value == 0


@_destructive_test
def test_smoke1_mutating_command_exits_cleanly() -> None:
    """A successful mutating command must not fail during close/disconnect cleanup."""
    return_value, out = _run(
        "meshtastic",
        "--ch-set",
        "name",
        "ExitClean",
        "--ch-index",
        "0",
    )
    _assert_connected(out)
    assert "Set name to ExitClean" in out
    assert "Writing modified channels to device" in out
    assert "Bad file descriptor" not in out
    assert "Aborting due to" not in out
    assert return_value == 0


@_destructive_test
def test_smoke1_reboot() -> None:
    """`--reboot` should return success and the node should come back."""
    return_value, _ = _run("meshtastic", "--reboot")
    assert return_value == 0
    info_code, info_out = _wait_for_info_ready()
    _assert_connected(info_out)
    assert info_code == 0


@_destructive_test
def test_smoke1_pos_fields_supported_values() -> None:
    """`--pos-fields` should set and read currently supported position flags."""
    return_value, out = _run(
        "meshtastic",
        "--pos-fields",
        "ALTITUDE",
        "ALTITUDE_MSL",
        "DOP",
    )
    _assert_connected(out)
    assert re.search(r"^Setting position fields to", out, re.MULTILINE)
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, out = _run("meshtastic", "--pos-fields")
    _assert_connected(out)
    assert "ALTITUDE" in out
    assert "ALTITUDE_MSL" in out
    assert "DOP" in out
    assert return_value == 0


@_destructive_test
def test_smoke1_set_location_info() -> None:
    """`--setlat/--setlon/--setalt` should update fixed position values."""
    return_value, out = _run(
        "meshtastic",
        "--setlat",
        "32.7767",
        "--setlon",
        "-96.7970",
        "--setalt",
        "1337",
    )
    _assert_connected(out)
    assert "Fixing altitude" in out
    assert "Fixing latitude" in out
    assert "Fixing longitude" in out
    assert "Setting device position" in out
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, out2 = _run("meshtastic", "--info")
    assert "1337" in out2
    assert "32.7767" in out2
    assert "-96.797" in out2
    assert return_value == 0


@_destructive_test
def test_smoke1_set_owner() -> None:
    """`--set-owner` should modify owner display text."""
    return_value, out = _run("meshtastic", "--set-owner", "Bob")
    _assert_connected(out)
    assert re.search(r"^Setting device owner to Bob", out, re.MULTILINE)
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, out = _run("meshtastic", "--info")
    assert re.search(r"^Owner: Bob\b", out, re.MULTILINE)
    assert return_value == 0


@_destructive_test
def test_smoke1_ch_set_modem_config_reports_unsupported() -> None:
    """`--ch-set modem_config` should fail gracefully on modern channel schema."""
    return_value, out = _run(
        "meshtastic",
        "--ch-set",
        "modem_config",
        "MedFast",
        "--ch-index",
        "0",
    )
    _assert_connected(out)
    assert "does not have an attribute modem_config" in out
    assert "Choices are..." in out
    assert return_value == 0


@_destructive_test
@pytest.mark.parametrize(
    ("preset_cmd", "expected_preset"),
    list(CHANNEL_PRESET_INFO_PATTERNS.items()),
)
def test_smoke1_ch_values(preset_cmd: str, expected_preset: str) -> None:
    """Channel preset switches should apply and be reflected in `--info`."""
    return_value, out = _run("meshtastic", preset_cmd)
    _assert_connected(out)
    assert "Writing modified channels to device" in out
    assert return_value == 0

    info_code, info_out = _wait_for_info_ready()
    _assert_connected(info_out)
    assert re.search(expected_preset, info_out, re.MULTILINE)
    assert info_code == 0


@_destructive_test
def test_smoke1_ch_set_name() -> None:
    """`--ch-set name` should update the selected channel name."""
    return_value, out = _run(
        "meshtastic",
        "--ch-set",
        "name",
        "MyChannel",
        "--ch-index",
        "0",
    )
    _assert_connected(out)
    assert re.search(r"^Set name to MyChannel", out, re.MULTILINE)
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, out = _run("meshtastic", "--info")
    assert "MyChannel" in out
    assert return_value == 0


@_destructive_test
def test_smoke1_ch_set_downlink_and_uplink() -> None:
    """`--ch-set downlink_enabled/uplink_enabled` should toggle and persist."""
    return_value, out = _run(
        "meshtastic",
        "--ch-set",
        "downlink_enabled",
        "false",
        "--ch-set",
        "uplink_enabled",
        "false",
        "--ch-index",
        "0",
    )
    _assert_connected(out)
    assert "Set downlink_enabled to false" in out
    assert "Set uplink_enabled to false" in out
    assert return_value == 0

    info_code, info_out = _wait_for_info_ready(
        timeout=PAUSE_AFTER_COMMAND + INFO_READY_TIMEOUT_SECONDS
    )
    _assert_connected(info_out)
    assert not re.search(r"uplinkEnabled", info_out, re.MULTILINE)
    assert not re.search(r"downlinkEnabled", info_out, re.MULTILINE)
    assert info_code == 0

    return_value, out = _run(
        "meshtastic",
        "--ch-set",
        "downlink_enabled",
        "true",
        "--ch-set",
        "uplink_enabled",
        "true",
        "--ch-index",
        "0",
    )
    _assert_connected(out)
    assert "Set downlink_enabled to true" in out
    assert "Set uplink_enabled to true" in out
    assert return_value == 0

    info_code, info_out = _wait_for_info_ready(
        timeout=PAUSE_AFTER_COMMAND + INFO_READY_TIMEOUT_SECONDS
    )
    _assert_connected(info_out)
    assert re.search(r"uplinkEnabled", info_out, re.MULTILINE)
    assert re.search(r"downlinkEnabled", info_out, re.MULTILINE)
    assert info_code == 0


@_destructive_test
def test_smoke1_ch_add_and_ch_del() -> None:
    """`--ch-add/--ch-del` should add then remove a uniquely named channel."""
    channel_name = _unique_channel_name("add")
    return_value, out = _run("meshtastic", "--ch-add", channel_name)
    _assert_connected(out)
    assert "Writing modified channels to device" in out
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    idx = _extract_added_channel_index(out)
    info_return, info_out = _run("meshtastic", "--info")
    assert info_return == 0
    assert channel_name in info_out
    if idx is None:
        idx = _find_channel_index_by_name(info_out, channel_name)
    assert idx is not None

    return_value, out = _run("meshtastic", "--ch-del", "--ch-index", str(idx))
    assert "Deleting channel" in out
    assert return_value == 0
    time.sleep(PAUSE_AFTER_REBOOT)

    return_value, out = _run("meshtastic", "--info")
    assert return_value == 0
    assert channel_name not in out


@_destructive_test
def test_smoke1_ch_enable_and_disable() -> None:
    """A non-primary channel should be disable-able and re-enable-able."""
    channel_name = _unique_channel_name("ena")
    return_value, out = _run("meshtastic", "--ch-add", channel_name)
    assert return_value == 0
    idx = _extract_added_channel_index(out)

    time.sleep(PAUSE_AFTER_COMMAND)
    info_return, info_out = _run("meshtastic", "--info")
    assert info_return == 0
    if idx is None:
        idx = _find_channel_index_by_name(info_out, channel_name)
    assert idx is not None

    return_value, out = _run("meshtastic", "--ch-disable", "--ch-index", str(idx))
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    info_return, info_out = _run("meshtastic", "--info")
    assert info_return == 0
    assert re.search(rf"^\s*Index\s+{idx}:\s+DISABLED", info_out, re.MULTILINE)

    return_value, out = _run("meshtastic", "--ch-enable", "--ch-index", str(idx))
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    info_return, info_out = _run("meshtastic", "--info")
    assert info_return == 0
    assert channel_name in info_out

    cleanup_return_value, cleanup_out = _run(
        "meshtastic",
        "--ch-del",
        "--ch-index",
        str(idx),
    )
    assert cleanup_return_value == 0, cleanup_out


@_destructive_test
def test_smoke1_ch_del_a_disabled_non_primary_channel() -> None:
    """Deleting a disabled non-primary channel should succeed."""
    channel_name = _unique_channel_name("dld")
    return_value, out = _run("meshtastic", "--ch-add", channel_name)
    assert return_value == 0
    idx = _extract_added_channel_index(out)

    time.sleep(PAUSE_AFTER_COMMAND)
    info_return, info_out = _run("meshtastic", "--info")
    assert info_return == 0
    if idx is None:
        idx = _find_channel_index_by_name(info_out, channel_name)
    assert idx is not None

    return_value, _ = _run("meshtastic", "--ch-disable", "--ch-index", str(idx))
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, _ = _run("meshtastic", "--ch-del", "--ch-index", str(idx))
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    info_return, info_out = _run("meshtastic", "--info")
    assert info_return == 0
    assert channel_name not in info_out


@_destructive_test
def test_smoke1_attempt_to_delete_primary_channel() -> None:
    """Deleting PRIMARY should be rejected."""
    return_value, out = _run("meshtastic", "--ch-del", "--ch-index", "0")
    assert re.search(r"Warning:\s+Cannot delete primary channel", out)
    assert return_value == 1


@_destructive_test
def test_smoke1_attempt_to_disable_primary_channel() -> None:
    """Disabling PRIMARY should be rejected."""
    return_value, out = _run("meshtastic", "--ch-disable", "--ch-index", "0")
    assert re.search(r"Warning:\s+Cannot (disable|enable) primary channel", out)
    assert return_value == 1


@_destructive_test
def test_smoke1_attempt_to_enable_primary_channel() -> None:
    """Enabling PRIMARY should be rejected."""
    return_value, out = _run("meshtastic", "--ch-enable", "--ch-index", "0")
    assert re.search(r"Warning:\s+Cannot enable primary channel", out)
    assert return_value == 1


@_destructive_test
def test_smoke1_ensure_ch_del_second_of_three_channels() -> None:
    """Deleting the first of two newly-added channels should preserve the second."""
    name_a = _unique_channel_name("a")
    name_b = _unique_channel_name("b")
    rc_a, out_a = _run("meshtastic", "--ch-add", name_a)
    assert rc_a == 0
    idx_a = _extract_added_channel_index(out_a)

    time.sleep(PAUSE_AFTER_COMMAND)
    rc_b, out_b = _run("meshtastic", "--ch-add", name_b)
    assert rc_b == 0
    idx_b = _extract_added_channel_index(out_b)

    if idx_a is None or idx_b is None:
        _, info_out = _run("meshtastic", "--info")
        idx_a = _find_channel_index_by_name(info_out, name_a)
        idx_b = _find_channel_index_by_name(info_out, name_b)
    assert idx_a is not None
    assert idx_b is not None

    rc_del, _ = _run("meshtastic", "--ch-del", "--ch-index", str(idx_a))
    assert rc_del == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    rc_info, out_info = _run("meshtastic", "--info")
    assert rc_info == 0
    assert name_b in out_info

    idx_b_now = _find_channel_index_by_name(out_info, name_b)
    assert idx_b_now is not None
    cleanup_return_value, cleanup_out = _run(
        "meshtastic",
        "--ch-del",
        "--ch-index",
        str(idx_b_now),
    )
    assert cleanup_return_value == 0, cleanup_out


@_destructive_test
def test_smoke1_ensure_ch_del_third_of_three_channels() -> None:
    """Deleting the second of two newly-added channels should preserve the first."""
    name_a = _unique_channel_name("c")
    name_b = _unique_channel_name("d")
    rc_a, out_a = _run("meshtastic", "--ch-add", name_a)
    assert rc_a == 0
    idx_a = _extract_added_channel_index(out_a)

    time.sleep(PAUSE_AFTER_COMMAND)
    rc_b, out_b = _run("meshtastic", "--ch-add", name_b)
    assert rc_b == 0
    idx_b = _extract_added_channel_index(out_b)

    if idx_a is None or idx_b is None:
        _, info_out = _run("meshtastic", "--info")
        idx_a = _find_channel_index_by_name(info_out, name_a)
        idx_b = _find_channel_index_by_name(info_out, name_b)
    assert idx_a is not None
    assert idx_b is not None

    rc_del, _ = _run("meshtastic", "--ch-del", "--ch-index", str(idx_b))
    assert rc_del == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    rc_info, out_info = _run("meshtastic", "--info")
    assert rc_info == 0
    assert name_a in out_info

    idx_a_now = _find_channel_index_by_name(out_info, name_a)
    assert idx_a_now is not None
    cleanup_return_value, cleanup_out = _run(
        "meshtastic",
        "--ch-del",
        "--ch-index",
        str(idx_a_now),
    )
    assert cleanup_return_value == 0, cleanup_out


@_destructive_test
def test_smoke1_seturl_default() -> None:
    """`--seturl` with the default URL should restore the default URL token."""
    return_value, out = _run(
        "meshtastic",
        "--ch-set",
        "name",
        "foo",
        "--ch-index",
        "0",
    )
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, out = _run("meshtastic", "--info")
    assert return_value == 0
    assert DEFAULT_URL_FRAGMENT not in out

    url = "https://www.meshtastic.org/d/#CgUYAyIBAQ"
    return_value, out = _run("meshtastic", "--seturl", url)
    _assert_connected(out)
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, out = _run("meshtastic", "--info")
    assert return_value == 0
    assert DEFAULT_URL_FRAGMENT in out


@_destructive_test
def test_smoke1_seturl_invalid_url() -> None:
    """Invalid URL should fail with a clear `There were no settings.` message."""
    url = "https://www.meshtastic.org/c/#GAMiENTxuzogKQdZ8Lz_q89Oab8qB0RlZmF1bHQ="
    return_value, out = _run("meshtastic", "--seturl", url)
    assert "There were no settings." in out
    assert return_value == 1


@pytest.mark.examples
@_destructive_test
def test_smoke1_configure() -> None:
    """`--configure example_config.yaml` should apply canonical snake_case config."""
    config_path = Path(__file__).resolve().parents[2] / "example_config.yaml"
    assert config_path.exists(), f"Config file not found: {config_path}"
    return_value, out = _run("meshtastic", "--configure", str(config_path))
    _assert_connected(out)
    assert re.search(r"^Setting device owner to Bob TBeam", out, re.MULTILINE)
    assert re.search(r"^Fixing altitude at 304 meters", out, re.MULTILINE)
    assert re.search(r"^Fixing latitude at 35\.88888", out, re.MULTILINE)
    assert re.search(r"^Fixing longitude at -93\.88888", out, re.MULTILINE)
    assert "Set lora.region to US" in out
    assert "Set power.wait_bluetooth_secs to 60" in out
    assert "Writing modified configuration to device" in out
    assert return_value == 0
    time.sleep(PAUSE_AFTER_REBOOT)


@_destructive_test
def test_smoke1_set_ham() -> None:
    """`--set-ham` should set a licensed owner and keep command success."""
    return_value, out = _run("meshtastic", "--set-ham", "KI1234")
    assert "Setting Ham ID" in out
    assert return_value == 0
    return_value, out = _wait_for_info_ready()
    _assert_connected(out)
    assert re.search(r"Owner: KI1234", out, re.MULTILINE)
    assert return_value == 0


@_destructive_test
def test_smoke1_set_wifi_settings() -> None:
    """`network.wifi_ssid`/`network.wifi_psk` should set and report expected values."""
    return_value, out = _run(
        "meshtastic",
        "--set",
        "network.wifi_ssid",
        "some_ssid",
        "--set",
        "network.wifi_psk",
        "temp1234",
    )
    _assert_connected(out)
    assert re.search(r"^Set network\.wifi_ssid to some_ssid", out, re.MULTILINE)
    assert re.search(r"^Set network\.wifi_psk to temp1234", out, re.MULTILINE)
    assert return_value == 0
    time.sleep(PAUSE_AFTER_COMMAND)

    return_value, out = _run(
        "meshtastic",
        "--get",
        "network.wifi_ssid",
        "--get",
        "network.wifi_psk",
    )
    assert re.search(r"network\.wifi_ssid:\s+some_ssid", out, re.MULTILINE)
    assert re.search(r"network\.wifi_psk:\s+sekrit", out, re.MULTILINE)
    assert return_value == 0


@_destructive_test
def test_smoke1_factory_reset() -> None:
    """`--factory-reset` should execute successfully and reboot the node."""
    return_value, out = _run("meshtastic", "--factory-reset")
    _assert_connected(out)
    assert "Aborting due to" not in out
    assert return_value == 0
    info_code, info_out = _wait_for_info_ready()
    _assert_connected(info_out)
    assert info_code == 0
