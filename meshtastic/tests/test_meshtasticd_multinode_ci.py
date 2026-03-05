"""Dual-daemon meshtasticd integration checks for CI."""

import os
import re
import time
from pathlib import Path

import pytest
import yaml

from .cli_test_utils import _run_host_cli, _run_host_cli_ok

pytestmark = pytest.mark.int

HOST_A = os.environ.get("MESHTASTICD_HOST_A", "localhost:4403")
HOST_B = os.environ.get("MESHTASTICD_HOST_B", "localhost:4404")
PRIMARY_CHANNEL_NAME = "CIPrimary"
LONGFAST_SECONDARY_NAME = "LongFast"
SECONDARY_CHANNEL_NAMES = [
    "CIChan2",
    "CIChan3",
    "CIChan4",
    "CIChan5",
    "CIChan6",
    "CIChan7",
]
CONFIGURED_OWNER = "CI Multinode"
CONFIGURED_OWNER_SHORT = "CIM"
INFO_CHANNEL_LINE_RE = re.compile(
    r'^\s*Index (?P<idx>\d+): (?P<role>PRIMARY|SECONDARY).*"name": "(?P<name>[^"]*)"',
    re.MULTILINE,
)


def _wait_for_host_ready(host: str, timeout_seconds: float = 60.0) -> None:
    """Wait until host responds successfully to --info."""
    deadline = time.monotonic() + timeout_seconds
    last_output = ""
    while time.monotonic() < deadline:
        returncode, output = _run_host_cli(host, "--info", timeout=10)
        if returncode == 0 and "Connected to radio" in output:
            return
        last_output = output
        time.sleep(1)
    pytest.fail(
        f"{host} did not become ready in {timeout_seconds}s.\nLast output:\n{last_output}"
    )


def _extract_channel_names(info_output: str) -> dict[int, str]:
    """Extract index->name mapping from the --info channels table."""
    channels: dict[int, str] = {}
    for match in INFO_CHANNEL_LINE_RE.finditer(info_output):
        channels[int(match.group("idx"))] = match.group("name")
    return channels


def _configure_channel_blueprint(host: str) -> dict[int, str]:
    """Configure 8 channels with deterministic names/settings on one daemon."""
    _run_host_cli_ok(host, "--set", "lora.region", "US")
    _run_host_cli_ok(host, "--set", "lora.channel_num", "20")
    _run_host_cli_ok(host, "--ch-set", "name", PRIMARY_CHANNEL_NAME, "--ch-index", "0")
    _run_host_cli_ok(
        host,
        "--ch-set",
        "module_settings.position_precision",
        "0",
        "--ch-index",
        "0",
    )

    expected_channel_names = {0: PRIMARY_CHANNEL_NAME, 1: LONGFAST_SECONDARY_NAME}
    _run_host_cli_ok(host, "--ch-add", LONGFAST_SECONDARY_NAME)
    _run_host_cli_ok(host, "--ch-set", "psk", "none", "--ch-index", "1")
    _run_host_cli_ok(
        host,
        "--ch-set",
        "module_settings.position_precision",
        "13",
        "--ch-index",
        "1",
    )
    _run_host_cli_ok(host, "--ch-set", "uplink_enabled", "true", "--ch-index", "1")
    _run_host_cli_ok(host, "--ch-set", "downlink_enabled", "false", "--ch-index", "1")

    for index, channel_name in enumerate(SECONDARY_CHANNEL_NAMES, start=2):
        _run_host_cli_ok(host, "--ch-add", channel_name)
        _run_host_cli_ok(host, "--ch-set", "psk", "random", "--ch-index", str(index))
        _run_host_cli_ok(
            host,
            "--ch-set",
            "module_settings.position_precision",
            "0",
            "--ch-index",
            str(index),
        )
        expected_channel_names[index] = channel_name
        uplink_enabled = "true" if index % 2 == 0 else "false"
        downlink_enabled = "false" if index % 2 == 0 else "true"
        _run_host_cli_ok(
            host,
            "--ch-set",
            "uplink_enabled",
            uplink_enabled,
            "--ch-index",
            str(index),
        )
        _run_host_cli_ok(
            host,
            "--ch-set",
            "downlink_enabled",
            downlink_enabled,
            "--ch-index",
            str(index),
        )

    info_output = _run_host_cli_ok(host, "--info")
    assert "Primary channel URL:" in info_output
    assert "Complete URL (includes all channels):" in info_output
    assert _extract_channel_names(info_output) == expected_channel_names
    assert re.search(
        rf'^\s*Index 0: PRIMARY psk=default .*"name": "{re.escape(PRIMARY_CHANNEL_NAME)}".*"positionPrecision": 0',
        info_output,
        re.MULTILINE,
    )
    assert re.search(
        rf'^\s*Index 1: SECONDARY psk=unencrypted .*"name": "{re.escape(LONGFAST_SECONDARY_NAME)}".*"positionPrecision": 13',
        info_output,
        re.MULTILINE,
    )
    for index, channel_name in enumerate(SECONDARY_CHANNEL_NAMES, start=2):
        assert re.search(
            rf'^\s*Index {index}: SECONDARY psk=secret .*"name": "{re.escape(channel_name)}"',
            info_output,
            re.MULTILINE,
        )

    region_output = _run_host_cli_ok(host, "--get", "lora.region")
    assert re.search(r"^lora\.region:\s+(US|1)$", region_output, re.MULTILINE)
    channel_num_output = _run_host_cli_ok(host, "--get", "lora.channel_num")
    assert re.search(r"^lora\.channel_num:\s+20$", channel_num_output, re.MULTILINE)

    return expected_channel_names


def _assert_admin_commands(host: str) -> None:
    """Admin and sendtext operations should succeed on one daemon instance."""
    metadata_output = _run_host_cli_ok(host, "--device-metadata")
    assert "firmware_version:" in metadata_output
    get_output = _run_host_cli_ok(host, "--get", "lora.region")
    assert re.search(r"^lora\.region:\s+", get_output, re.MULTILINE)
    send_text_output = _run_host_cli_ok(host, "--sendtext", f"ci-admin-check-{host}")
    assert "Sending text message" in send_text_output


def test_meshtasticd_multinode_channel_blueprint_export_and_reuse(
    tmp_path: Path,
) -> None:
    """Exercise admin commands, then export/restore config across two simulators."""
    _wait_for_host_ready(HOST_A)
    _wait_for_host_ready(HOST_B)

    for host in (HOST_A, HOST_B):
        _assert_admin_commands(host)

    _configure_channel_blueprint(HOST_A)

    export_path = tmp_path / "meshtasticd-multinode-export.yaml"
    _run_host_cli_ok(HOST_A, "--export-config", str(export_path))
    assert export_path.exists()
    assert export_path.stat().st_size > 0

    exported_data = yaml.safe_load(export_path.read_text(encoding="utf-8"))
    assert isinstance(exported_data, dict)
    source_channel_url = exported_data.get("channel_url")
    assert isinstance(source_channel_url, str)
    assert source_channel_url.startswith("https://meshtastic.org/e/#")
    exported_data["owner"] = CONFIGURED_OWNER
    exported_data["owner_short"] = CONFIGURED_OWNER_SHORT
    export_path.write_text(
        yaml.safe_dump(exported_data, sort_keys=False),
        encoding="utf-8",
    )

    configure_output = _run_host_cli_ok(
        HOST_B, "--configure", str(export_path), timeout=45
    )
    assert "Writing modified configuration to device" in configure_output
    _wait_for_host_ready(HOST_B, timeout_seconds=90.0)

    info_output_b = _run_host_cli_ok(HOST_B, "--info")
    assert re.search(
        rf"^Owner:\s+{re.escape(CONFIGURED_OWNER)}\b", info_output_b, re.MULTILINE
    )
    region_output_b = _run_host_cli_ok(HOST_B, "--get", "lora.region")
    assert re.search(r"^lora\.region:\s+(US|1)$", region_output_b, re.MULTILINE)
    channel_num_output_b = _run_host_cli_ok(HOST_B, "--get", "lora.channel_num")
    assert re.search(r"^lora\.channel_num:\s+20$", channel_num_output_b, re.MULTILINE)

    export_path_b = tmp_path / "meshtasticd-multinode-export-b.yaml"
    _run_host_cli_ok(HOST_B, "--export-config", str(export_path_b))
    exported_data_b = yaml.safe_load(export_path_b.read_text(encoding="utf-8"))
    assert isinstance(exported_data_b, dict)
    channel_url_b = exported_data_b.get("channel_url")
    assert isinstance(channel_url_b, str)
    assert channel_url_b.startswith("https://meshtastic.org/e/#")
    assert len(channel_url_b) >= 120
