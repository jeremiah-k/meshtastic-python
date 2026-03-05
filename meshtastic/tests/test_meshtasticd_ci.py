"""Stable meshtasticd integration checks for CI."""

import re
from pathlib import Path

import pytest
import yaml

from .cli_test_utils import _run_host_cli_ok

pytestmark = pytest.mark.int


def test_meshtasticd_info_has_core_sections(meshtastic_bin: str) -> None:
    """`--info` should connect and return the key high-level sections."""
    output = _run_host_cli_ok("localhost", "--info", meshtastic_bin=meshtastic_bin)
    assert output.startswith("Connected to radio")
    assert re.search(r"^Owner:", output, re.MULTILINE)
    assert re.search(r"^My info:", output, re.MULTILINE)
    assert re.search(r"^Channels", output, re.MULTILINE)


def test_meshtasticd_nodes_lists_local_node(meshtastic_bin: str) -> None:
    """`--nodes` should include at least one Meshtastic node id."""
    output = _run_host_cli_ok("localhost", "--nodes", meshtastic_bin=meshtastic_bin)
    assert output.startswith("Connected to radio")
    assert re.search(r"![0-9a-fA-F]{8}", output)


def test_meshtasticd_export_and_configure_roundtrip(
    meshtastic_bin: str, tmp_path: Path
) -> None:
    """Export, mutate, configure, verify live state, then restore baseline config."""
    export_path = tmp_path / "meshtasticd-ci-export.yaml"
    mutated_path = tmp_path / "meshtasticd-ci-mutated.yaml"

    export_output = _run_host_cli_ok(
        "localhost",
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
            "localhost",
            "--configure",
            str(mutated_path),
            meshtastic_bin=meshtastic_bin,
        )
        assert "Writing modified configuration to device" in configure_output

        info_output = _run_host_cli_ok(
            "localhost",
            "--info",
            meshtastic_bin=meshtastic_bin,
        )
        assert info_output.startswith("Connected to radio")
        assert re.search(r"^Owner: Meshtastic CI\b", info_output, re.MULTILINE)
    finally:
        restore_output = _run_host_cli_ok(
            "localhost",
            "--configure",
            str(export_path),
            meshtastic_bin=meshtastic_bin,
        )
        assert "Writing modified configuration to device" in restore_output


def test_meshtasticd_get_and_sendtext_paths(meshtastic_bin: str) -> None:
    """Read-only `--get` and broadcast `--sendtext` should succeed."""
    get_output = _run_host_cli_ok(
        "localhost",
        "--get",
        "lora.region",
        meshtastic_bin=meshtastic_bin,
    )
    assert re.search(r"^lora\.region:", get_output, re.MULTILINE)

    send_output = _run_host_cli_ok(
        "localhost",
        "--sendtext",
        "hello",
        meshtastic_bin=meshtastic_bin,
    )
    assert "Sending text message" in send_output
