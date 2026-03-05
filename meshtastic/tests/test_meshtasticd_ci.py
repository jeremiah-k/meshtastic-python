"""Stable meshtasticd integration checks for CI."""

import re
from pathlib import Path

import pytest
import yaml

from .cli_test_utils import run_cli_argv_with_timeout

pytestmark = pytest.mark.int


def _run_host_cli(meshtastic_bin: str, *args: str) -> tuple[int, str]:
    """Run a meshtastic CLI command against localhost and return (code, output)."""
    result = run_cli_argv_with_timeout([meshtastic_bin, "--host", "localhost", *args])
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def test_meshtasticd_info_has_core_sections(meshtastic_bin: str) -> None:
    """`--info` should connect and return the key high-level sections."""
    returncode, output = _run_host_cli(meshtastic_bin, "--info")
    assert returncode == 0
    assert output.startswith("Connected to radio")
    assert re.search(r"^Owner:", output, re.MULTILINE)
    assert re.search(r"^My info:", output, re.MULTILINE)
    assert re.search(r"^Channels", output, re.MULTILINE)


def test_meshtasticd_nodes_lists_local_node(meshtastic_bin: str) -> None:
    """`--nodes` should include at least one Meshtastic node id."""
    returncode, output = _run_host_cli(meshtastic_bin, "--nodes")
    assert returncode == 0
    assert output.startswith("Connected to radio")
    assert re.search(r"![0-9a-fA-F]{8}", output)


def test_meshtasticd_export_and_configure_roundtrip(
    meshtastic_bin: str, tmp_path: Path
) -> None:
    """Export, mutate, configure, verify live state, then restore baseline config."""
    export_path = tmp_path / "meshtasticd-ci-export.yaml"
    mutated_path = tmp_path / "meshtasticd-ci-mutated.yaml"

    export_code, export_output = _run_host_cli(
        meshtastic_bin, "--export-config", str(export_path)
    )
    assert export_code == 0
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

    configure_code, configure_output = _run_host_cli(
        meshtastic_bin, "--configure", str(mutated_path)
    )
    assert configure_code == 0
    assert "Writing modified configuration to device" in configure_output

    info_code, info_output = _run_host_cli(meshtastic_bin, "--info")
    assert info_code == 0
    assert info_output.startswith("Connected to radio")
    assert re.search(r"^Owner: Meshtastic CI\b", info_output, re.MULTILINE)

    restore_code, restore_output = _run_host_cli(
        meshtastic_bin, "--configure", str(export_path)
    )
    assert restore_code == 0
    assert "Writing modified configuration to device" in restore_output


def test_meshtasticd_get_and_sendtext_paths(meshtastic_bin: str) -> None:
    """Read-only `--get` and broadcast `--sendtext` should succeed."""
    get_code, get_output = _run_host_cli(meshtastic_bin, "--get", "lora.region")
    assert get_code == 0
    assert re.search(r"^lora\.region:", get_output, re.MULTILINE)

    send_code, send_output = _run_host_cli(meshtastic_bin, "--sendtext", "hello")
    assert send_code == 0
    assert "Sending text message" in send_output
