"""Stable meshtasticd integration checks for CI."""

import os
import re
import time
from pathlib import Path

import pytest
import yaml

from .cli_test_utils import _run_host_cli, _run_host_cli_ok

pytestmark = [pytest.mark.int, pytest.mark.smokevirt]

HOST = os.environ.get("MESHTASTICD_HOST", "localhost")
HOST_READY_TIMEOUT_SECONDS = 45.0
HOST_READY_POLL_TIMEOUT_SECONDS = 10


def _wait_for_host_ready(host: str, meshtastic_bin: str) -> None:
    """Wait for a host to return a successful ``--info`` response."""
    deadline = time.monotonic() + HOST_READY_TIMEOUT_SECONDS
    last_output = ""
    while time.monotonic() < deadline:
        returncode, output = _run_host_cli(
            host,
            "--info",
            timeout=HOST_READY_POLL_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        if returncode == 0 and output.startswith("Connected to radio"):
            return
        last_output = output
        time.sleep(1)
    pytest.fail(
        f"{host} did not become ready in {HOST_READY_TIMEOUT_SECONDS}s."
        f"\nLast output:\n{last_output}"
    )


def test_meshtasticd_info_has_core_sections(meshtastic_bin: str) -> None:
    """`--info` should connect and return the key high-level sections."""
    output = _run_host_cli_ok(HOST, "--info", meshtastic_bin=meshtastic_bin)
    assert output.startswith("Connected to radio")
    assert re.search(r"^Owner:", output, re.MULTILINE)
    assert re.search(r"^My info:", output, re.MULTILINE)
    assert re.search(r"^Channels", output, re.MULTILINE)


def test_meshtasticd_nodes_lists_local_node(meshtastic_bin: str) -> None:
    """`--nodes` should include at least one Meshtastic node id."""
    output = _run_host_cli_ok(HOST, "--nodes", meshtastic_bin=meshtastic_bin)
    assert output.startswith("Connected to radio")
    assert re.search(r"![0-9a-fA-F]{8}", output)


def test_meshtasticd_export_and_configure_roundtrip(
    meshtastic_bin: str, tmp_path: Path
) -> None:
    """Export, mutate, configure, verify live state, then restore baseline config."""
    export_path = tmp_path / "meshtasticd-ci-export.yaml"
    mutated_path = tmp_path / "meshtasticd-ci-mutated.yaml"

    export_output = _run_host_cli_ok(
        HOST,
        "--export-config",
        str(export_path),
        meshtastic_bin=meshtastic_bin,
    )
    assert "Exported configuration to" in export_output
    assert export_path.exists()
    assert export_path.stat().st_size > 0

    exported_data = yaml.safe_load(export_path.read_text(encoding="utf-8"))
    assert isinstance(exported_data, dict)
    mutated_data = dict(exported_data)
    mutated_data["owner"] = "Meshtastic CI"
    mutated_data["owner_short"] = "MCI"
    mutated_path.write_text(
        yaml.safe_dump(mutated_data, sort_keys=False), encoding="utf-8"
    )

    try:
        configure_output = _run_host_cli_ok(
            HOST,
            "--configure",
            str(mutated_path),
            meshtastic_bin=meshtastic_bin,
        )
        assert "Writing modified configuration to device" in configure_output

        _wait_for_host_ready(HOST, meshtastic_bin)
        info_output = _run_host_cli_ok(
            HOST,
            "--info",
            meshtastic_bin=meshtastic_bin,
        )
        assert info_output.startswith("Connected to radio")
        assert re.search(r"^Owner: Meshtastic CI\b", info_output, re.MULTILINE)
    finally:
        restore_output = _run_host_cli_ok(
            HOST,
            "--configure",
            str(export_path),
            meshtastic_bin=meshtastic_bin,
        )
        assert "Writing modified configuration to device" in restore_output
        _wait_for_host_ready(HOST, meshtastic_bin)


def test_meshtasticd_get_and_sendtext_paths(meshtastic_bin: str) -> None:
    """Read-only `--get` and broadcast `--sendtext` should succeed."""
    get_output = _run_host_cli_ok(
        HOST,
        "--get",
        "lora.region",
        meshtastic_bin=meshtastic_bin,
    )
    assert re.search(r"^lora\.region:", get_output, re.MULTILINE)

    send_output = _run_host_cli_ok(
        HOST,
        "--sendtext",
        "hello",
        meshtastic_bin=meshtastic_bin,
    )
    assert "Sending text message" in send_output
