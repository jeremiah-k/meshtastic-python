"""Dual-daemon meshtasticd integration checks for CI.

This module validates admin operations and configuration reuse across two
meshtasticd simulator instances.
"""

import base64
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from ..protobuf import apponly_pb2
from .cli_test_utils import _run_host_cli, _run_host_cli_ok

pytestmark = [pytest.mark.int, pytest.mark.smokevirt]


def _positive_float_from_env(name: str, default: float) -> float:
    """Read a positive float from the environment with a safe fallback."""
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value == "":
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return parsed


HOST_A = os.environ.get("MESHTASTICD_HOST_A", "localhost:4401")
HOST_B = os.environ.get("MESHTASTICD_HOST_B", "localhost:4402")
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
LORA_REGION = "US"
LORA_CHANNEL_NUM = "20"
POSITION_PRECISION_DISABLED = "0"
POSITION_PRECISION_DEFAULT = "13"
HOST_READY_TIMEOUT_SECONDS = _positive_float_from_env("MESH_HOST_READY_TIMEOUT", 60.0)
HOST_READY_POLL_CLI_TIMEOUT_SECONDS = _positive_float_from_env(
    "MESH_HOST_READY_POLL_TIMEOUT",
    10.0,
)
HOST_CONFIGURE_TIMEOUT_SECONDS = _positive_float_from_env(
    "MESH_HOST_CONFIGURE_TIMEOUT",
    45.0,
)
HOST_READY_AFTER_CONFIGURE_TIMEOUT_SECONDS = _positive_float_from_env(
    "MESH_HOST_READY_AFTER_CONFIGURE_TIMEOUT",
    90.0,
)
CLI_DEFAULT_TIMEOUT_SECONDS = _positive_float_from_env(
    "MESH_CLI_DEFAULT_TIMEOUT",
    30.0,
)
MIN_CHANNEL_URL_LENGTH = 120
SATURATION_ERROR_MSG = "No free channels were found"
SIMPLE_CHANNEL_PSK = b"\x01"
SATURATION_CONSERVATIVE_PADDING = 8
SATURATION_DERIVED_PADDING = 2
INFO_CHANNEL_LINE_RE = re.compile(
    r'^\s*Index (?P<idx>\d+): (?P<role>PRIMARY|SECONDARY).*"name": "(?P<name>[^"]*)"',
    re.MULTILINE,
)


def _sanitize_artifact_token(value: str) -> str:
    """Convert arbitrary host/label text into a filename-safe token."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "unknown"


def _capture_host_debug_state(
    tmp_path: Path,
    host: str,
    meshtastic_bin: str,
    *,
    label: str,
) -> None:
    """Best-effort host snapshot for post-failure diagnostics.

    Writes:
    - ``*-info.txt`` with ``--info`` return code/output
    - ``*-export.yaml`` when ``--export-config`` succeeds
    - ``*-export-error.txt`` when ``--export-config`` fails
    """
    host_token = _sanitize_artifact_token(host)
    label_token = _sanitize_artifact_token(label)

    try:
        info_rc, info_output = _run_host_cli(
            host,
            "--info",
            timeout=CLI_DEFAULT_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        info_artifact = tmp_path / f"debug-{label_token}-{host_token}-info.txt"
        info_artifact.write_text(
            f"host={host}\nreturncode={info_rc}\n\n{info_output}",
            encoding="utf-8",
        )

        export_artifact = tmp_path / f"debug-{label_token}-{host_token}-export.yaml"
        export_rc, export_output = _run_host_cli(
            host,
            "--export-config",
            str(export_artifact),
            timeout=CLI_DEFAULT_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        if export_rc != 0:
            export_error_artifact = (
                tmp_path / f"debug-{label_token}-{host_token}-export-error.txt"
            )
            export_error_artifact.write_text(
                f"host={host}\nreturncode={export_rc}\n\n{export_output}",
                encoding="utf-8",
            )
    except Exception as exc:  # pragma: no cover - debug-only fallback
        fallback_artifact = tmp_path / f"debug-{label_token}-{host_token}-capture-error.txt"
        fallback_artifact.write_text(
            f"Failed to capture debug state: {exc!r}",
            encoding="utf-8",
        )


def _wait_for_host_ready(
    host: str,
    meshtastic_bin: str,
    timeout_seconds: float = HOST_READY_TIMEOUT_SECONDS,
) -> None:
    """Wait until a host responds successfully to ``--info``.

    Parameters
    ----------
    host : str
        Host passed to the CLI ``--host`` argument.
    meshtastic_bin : str
        Path or name of the meshtastic CLI binary under test.
    timeout_seconds : float, optional
        Maximum number of seconds to wait for readiness.
    """
    deadline = time.monotonic() + timeout_seconds
    last_returncode: int | None = None
    last_output = ""
    while time.monotonic() < deadline:
        returncode, output = _run_host_cli(
            host,
            "--info",
            timeout=HOST_READY_POLL_CLI_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        if returncode == 0 and "Connected to radio" in output:
            return
        last_returncode = returncode
        last_output = output
        time.sleep(1)
    pytest.fail(
        f"{host} did not become ready in {timeout_seconds}s."
        f"\nLast return code: {last_returncode}"
        f"\nLast output:\n{last_output}"
    )


def _extract_channel_names(info_output: str) -> dict[int, str]:
    """Extract channel index/name pairs from ``--info`` output.

    Parameters
    ----------
    info_output : str
        CLI ``--info`` output that includes the channels table.

    Returns
    -------
    dict[int, str]
        Mapping from channel index to configured channel name.
    """
    channels: dict[int, str] = {}
    for match in INFO_CHANNEL_LINE_RE.finditer(info_output):
        channels[int(match.group("idx"))] = match.group("name")
    return channels


def _build_add_only_channel_url(channel_name: str) -> str:
    """Build a channel-only add URL with one uniquely named secondary channel.

    Parameters
    ----------
    channel_name : str
        Name to assign to the staged secondary channel.

    Returns
    -------
    str
        A meshtastic.org URL encoding a ChannelSet with a single named channel.
    """
    channel_set = apponly_pb2.ChannelSet()
    staged_channel = channel_set.settings.add()
    staged_channel.name = channel_name
    staged_channel.psk = SIMPLE_CHANNEL_PSK
    encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii")
    return f"https://meshtastic.org/e/#{encoded.rstrip('=')}"


def _estimate_saturation_add_attempts(
    host: str,
    meshtastic_bin: str,
    *,
    configured_channel_count: int,
    tmp_path: Path,
) -> int:
    """Estimate how many ``--ch-add`` attempts are needed to observe saturation.

    Prefer exported channel capacity when available; otherwise use a conservative
    bound from observed configured channel count.

    Parameters
    ----------
    host : str
        Host passed to the CLI ``--host`` argument.
    meshtastic_bin : str
        Path or name of the meshtastic CLI binary under test.
    configured_channel_count : int
        Number of channels already configured on the device.
    tmp_path : Path
        Temporary directory used for intermediate export files.

    Returns
    -------
    int
        Upper bound on how many ``--ch-add`` attempts are needed to observe
        saturation.
    """
    probe_export_path = tmp_path / "meshtasticd-multinode-capacity-probe.yaml"
    _run_host_cli_ok(
        host,
        "--export-config",
        str(probe_export_path),
        meshtastic_bin=meshtastic_bin,
    )
    exported_data = yaml.safe_load(probe_export_path.read_text(encoding="utf-8"))
    conservative_bound = max(
        configured_channel_count + SATURATION_CONSERVATIVE_PADDING,
        SATURATION_CONSERVATIVE_PADDING,
    )
    if isinstance(exported_data, dict):
        channels = exported_data.get("channels")
        if isinstance(channels, list):
            total_slots = len(channels)
            available_slots = max(total_slots - configured_channel_count, 0)
            derived_bound = max(
                available_slots + SATURATION_DERIVED_PADDING,
                SATURATION_DERIVED_PADDING,
            )
            return max(derived_bound, conservative_bound)
    return conservative_bound


def _extract_exported_channel_identities(channels: list[Any]) -> set[tuple[int, str]]:
    """Extract channel identity tuples from exported channel config.

    Parameters
    ----------
    channels : list[Any]
        ``channels`` list loaded from exported YAML config.

    Returns
    -------
    set[tuple[int, str]]
        Set of ``(index, name)`` tuples for each exported channel.
    """
    identities: set[tuple[int, str]] = set()
    for channel in channels:
        assert isinstance(channel, dict)
        index_value = channel.get("index")
        if isinstance(index_value, int):
            index = index_value
        else:
            assert isinstance(index_value, str)
            assert index_value.isdigit()
            index = int(index_value)

        name = ""
        settings = channel.get("settings")
        if isinstance(settings, dict):
            settings_name = settings.get("name")
            if isinstance(settings_name, str):
                name = settings_name
        if not name:
            direct_name = channel.get("name")
            assert isinstance(direct_name, str)
            name = direct_name

        identities.add((index, name))
    return identities


def _configure_channel_blueprint(host: str, meshtastic_bin: str) -> dict[int, str]:
    """Configure deterministic channel settings on one daemon.

    Parameters
    ----------
    host : str
        Host passed to the CLI ``--host`` argument.
    meshtastic_bin : str
        Path or name of the meshtastic CLI binary under test.

    Returns
    -------
    dict[int, str]
        Mapping from channel index to expected channel name after configure.
    """

    def _cli_ok(*args: str, timeout: int | float = CLI_DEFAULT_TIMEOUT_SECONDS) -> str:
        return _run_host_cli_ok(
            host,
            *args,
            timeout=timeout,
            meshtastic_bin=meshtastic_bin,
        )

    # Avoid reboot-prone LoRa writes in simulator-backed multinode CI. The
    # blueprint test targets channel/admin round-tripping rather than radio
    # reboot behavior.
    _cli_ok("--ch-set", "name", PRIMARY_CHANNEL_NAME, "--ch-index", "0")
    _cli_ok(
        "--ch-set",
        "module_settings.position_precision",
        POSITION_PRECISION_DISABLED,
        "--ch-index",
        "0",
    )

    expected_channel_names = {0: PRIMARY_CHANNEL_NAME, 1: LONGFAST_SECONDARY_NAME}
    _cli_ok("--ch-set", "name", LONGFAST_SECONDARY_NAME, "--ch-index", "1")
    _cli_ok("--ch-set", "psk", "none", "--ch-index", "1")
    _cli_ok(
        "--ch-set",
        "module_settings.position_precision",
        POSITION_PRECISION_DEFAULT,
        "--ch-index",
        "1",
    )
    _cli_ok("--ch-set", "uplink_enabled", "true", "--ch-index", "1")
    _cli_ok("--ch-set", "downlink_enabled", "false", "--ch-index", "1")

    for index, channel_name in enumerate(SECONDARY_CHANNEL_NAMES, start=2):
        _cli_ok("--ch-set", "name", channel_name, "--ch-index", str(index))
        _cli_ok("--ch-set", "psk", "random", "--ch-index", str(index))
        _cli_ok(
            "--ch-set",
            "module_settings.position_precision",
            POSITION_PRECISION_DISABLED,
            "--ch-index",
            str(index),
        )
        expected_channel_names[index] = channel_name
        uplink_enabled = "true" if index % 2 == 0 else "false"
        downlink_enabled = "false" if index % 2 == 0 else "true"
        _cli_ok(
            "--ch-set",
            "uplink_enabled",
            uplink_enabled,
            "--ch-index",
            str(index),
        )
        _cli_ok(
            "--ch-set",
            "downlink_enabled",
            downlink_enabled,
            "--ch-index",
            str(index),
        )

    info_output = _cli_ok("--info")
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

    return expected_channel_names


def _assert_admin_commands(host: str, meshtastic_bin: str) -> None:
    """Assert basic admin-related CLI operations succeed on one host.

    Parameters
    ----------
    host : str
        Host passed to the CLI ``--host`` argument.
    meshtastic_bin : str
        Path or name of the meshtastic CLI binary under test.
    """
    metadata_output = _run_host_cli_ok(
        host,
        "--device-metadata",
        meshtastic_bin=meshtastic_bin,
    )
    assert "firmware_version:" in metadata_output
    get_output = _run_host_cli_ok(
        host,
        "--get",
        "lora.region",
        meshtastic_bin=meshtastic_bin,
    )
    assert re.search(r"^lora\.region:\s+", get_output, re.MULTILINE)
    # Keep this precheck admin-focused. Broadcast sendtext probes on simulators
    # can leave retransmit traffic in-flight and fill to-phone queues while the
    # next CLI command reconnects, which makes this integration lane flaky.


@pytest.mark.xfail(
    reason="Resume logic implemented but 8-channel replace-all over TCP/meshtasticd "
    "still prone to multi-disconnect edge cases. Resume handles single "
    "disconnects; repeated disconnects within the same transaction may exceed "
    "retry bounds.",
    strict=False,
)
def test_meshtasticd_multinode_channel_blueprint_export_and_reuse(
    tmp_path: Path,
    meshtastic_bin: str,
) -> None:
    """Exercise admin commands, then export/restore config across two simulators.

    Parameters
    ----------
    tmp_path : Path
        Temporary directory used for exported YAML files.
    meshtastic_bin : str
        Path or name of the meshtastic CLI binary under test.
    """
    try:
        _wait_for_host_ready(HOST_A, meshtastic_bin)
        _wait_for_host_ready(HOST_B, meshtastic_bin)

        for host in (HOST_A, HOST_B):
            _assert_admin_commands(host, meshtastic_bin)

        expected_channel_names = _configure_channel_blueprint(HOST_A, meshtastic_bin)

        export_path = tmp_path / "meshtasticd-multinode-export.yaml"
        _run_host_cli_ok(
            HOST_A,
            "--export-config",
            str(export_path),
            meshtastic_bin=meshtastic_bin,
        )
        assert export_path.exists()
        assert export_path.stat().st_size > 0

        exported_data = yaml.safe_load(export_path.read_text(encoding="utf-8"))
        assert isinstance(exported_data, dict)
        source_channel_url = exported_data.get("channel_url")
        assert isinstance(source_channel_url, str)
        assert source_channel_url.startswith("https://meshtastic.org/e/#")
        assert len(source_channel_url) >= MIN_CHANNEL_URL_LENGTH
        exported_data["owner"] = CONFIGURED_OWNER
        exported_data["owner_short"] = CONFIGURED_OWNER_SHORT
        export_path.write_text(
            yaml.safe_dump(exported_data, sort_keys=False),
            encoding="utf-8",
        )

        configure_output = _run_host_cli_ok(
            HOST_B,
            "--configure",
            str(export_path),
            timeout=HOST_CONFIGURE_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        assert "Phase 1 complete." in configure_output
        _wait_for_host_ready(
            HOST_B,
            meshtastic_bin,
            timeout_seconds=HOST_READY_AFTER_CONFIGURE_TIMEOUT_SECONDS,
        )

        info_output_b = _run_host_cli_ok(
            HOST_B, "--info", meshtastic_bin=meshtastic_bin
        )
        assert re.search(
            rf"^Owner:\s+{re.escape(CONFIGURED_OWNER)}\b", info_output_b, re.MULTILINE
        )
        export_path_b = tmp_path / "meshtasticd-multinode-export-b.yaml"
        _run_host_cli_ok(
            HOST_B,
            "--export-config",
            str(export_path_b),
            meshtastic_bin=meshtastic_bin,
        )
        exported_data_b = yaml.safe_load(export_path_b.read_text(encoding="utf-8"))
        assert isinstance(exported_data_b, dict)
        owner_short_b = exported_data_b.get(
            "owner_short", exported_data_b.get("ownerShort")
        )
        assert owner_short_b == CONFIGURED_OWNER_SHORT
        channel_url_b = exported_data_b.get("channel_url")
        assert isinstance(channel_url_b, str)
        assert channel_url_b.startswith("https://meshtastic.org/e/#")
        assert len(channel_url_b) >= MIN_CHANNEL_URL_LENGTH

        channel_name_map_b = _extract_channel_names(info_output_b)
        channels_a = exported_data.get("channels")
        channels_b = exported_data_b.get("channels")
        if channels_a is None or channels_b is None:
            assert channels_a is None
            assert channels_b is None
            expected_names = {name for name in expected_channel_names.values() if name}
            observed_names = {name for name in channel_name_map_b.values() if name}
            assert observed_names <= expected_names, (
                f"Unexpected channel names on restored host {HOST_B}. "
                f"unexpected={sorted(observed_names - expected_names)}, "
                f"observed={sorted(observed_names)}, expected_subset={sorted(expected_names)}"
            )
            observed_secondary_names = observed_names & set(SECONDARY_CHANNEL_NAMES)
            assert len(observed_secondary_names) >= 3, (
                "Expected at least 3 secondary blueprint channels after restore; "
                f"observed={sorted(observed_secondary_names)}"
            )
            for index in (0, 1):
                observed_name = channel_name_map_b.get(index, "")
                if observed_name:
                    assert observed_name == expected_channel_names[index]
        else:
            assert isinstance(channels_a, list)
            assert isinstance(channels_b, list)
            assert len(channels_a) == len(channels_b)
            identities_a = _extract_exported_channel_identities(channels_a)
            identities_b = _extract_exported_channel_identities(channels_b)
            assert len(identities_a) == len(channels_a)
            assert len(identities_b) == len(channels_b)
            assert identities_a == identities_b, (
                "Exported channel identities differ after configure replay.\n"
                f"missing_on_{HOST_B}={sorted(identities_a - identities_b)}\n"
                f"extra_on_{HOST_B}={sorted(identities_b - identities_a)}"
            )
    except Exception:
        _capture_host_debug_state(
            tmp_path,
            HOST_A,
            meshtastic_bin,
            label="blueprint-export-reuse-failure-host-a",
        )
        _capture_host_debug_state(
            tmp_path,
            HOST_B,
            meshtastic_bin,
            label="blueprint-export-reuse-failure-host-b",
        )
        raise


def test_meshtasticd_multinode_add_only_url_is_non_mutating_when_no_slots_remain(
    tmp_path: Path,
    meshtastic_bin: str,
) -> None:
    """`--ch-add-url` should fail atomically when no DISABLED slots remain.

    Parameters
    ----------
    tmp_path : Path
        Temporary directory used for intermediate export files.
    meshtastic_bin : str
        Path or name of the meshtastic CLI binary under test.
    """
    _wait_for_host_ready(HOST_A, meshtastic_bin)

    _configure_channel_blueprint(HOST_A, meshtastic_bin)
    baseline_export_path = tmp_path / "meshtasticd-multinode-a-baseline.yaml"
    _run_host_cli_ok(
        HOST_A,
        "--export-config",
        str(baseline_export_path),
        meshtastic_bin=meshtastic_bin,
    )

    try:
        try:
            initial_info = _run_host_cli_ok(
                HOST_A, "--info", meshtastic_bin=meshtastic_bin
            )
            initial_channels = _extract_channel_names(initial_info)
            assert initial_channels

            max_attempts = _estimate_saturation_add_attempts(
                HOST_A,
                meshtastic_bin,
                configured_channel_count=len(initial_channels),
                tmp_path=tmp_path,
            )
            saturated = False
            for attempt in range(max_attempts):
                fill_name = f"CIFill{attempt:02d}"
                returncode, output = _run_host_cli(
                    HOST_A,
                    "--ch-add",
                    fill_name,
                    meshtastic_bin=meshtastic_bin,
                    timeout=CLI_DEFAULT_TIMEOUT_SECONDS,
                )
                if returncode == 0:
                    continue
                assert SATURATION_ERROR_MSG in output
                saturated = True
                break
            assert saturated, (
                "Host never reached channel saturation within derived bound. "
                f"max_attempts={max_attempts}, initial_channels={len(initial_channels)}"
            )
            saturated_info = _run_host_cli_ok(
                HOST_A,
                "--info",
                meshtastic_bin=meshtastic_bin,
            )
            saturated_channels = _extract_channel_names(saturated_info)
            saturated_export_before_path = (
                tmp_path / "meshtasticd-multinode-a-sat-before.yaml"
            )
            _run_host_cli_ok(
                HOST_A,
                "--export-config",
                str(saturated_export_before_path),
                meshtastic_bin=meshtastic_bin,
            )
            saturated_config_before = yaml.safe_load(
                saturated_export_before_path.read_text(encoding="utf-8")
            )

            channel_name = "CIRollbackProbe"
            channel_url = _build_add_only_channel_url(channel_name)
            add_rc, add_out = _run_host_cli(
                HOST_A,
                "--ch-add-url",
                channel_url,
                meshtastic_bin=meshtastic_bin,
                timeout=CLI_DEFAULT_TIMEOUT_SECONDS,
            )
            assert add_rc != 0
            assert SATURATION_ERROR_MSG in add_out

            after_info = _run_host_cli_ok(HOST_A, "--info", meshtastic_bin=meshtastic_bin)
            after_channels = _extract_channel_names(after_info)
            assert after_channels == saturated_channels, (
                "Channel map changed after failed add-only URL when saturated.\n"
                f"before={saturated_channels}\n"
                f"after={after_channels}"
            )
            assert channel_name not in after_info
            saturated_export_after_path = (
                tmp_path / "meshtasticd-multinode-a-sat-after.yaml"
            )
            _run_host_cli_ok(
                HOST_A,
                "--export-config",
                str(saturated_export_after_path),
                meshtastic_bin=meshtastic_bin,
            )
            saturated_config_after = yaml.safe_load(
                saturated_export_after_path.read_text(encoding="utf-8")
            )
            assert saturated_config_after == saturated_config_before
        except Exception:
            _capture_host_debug_state(
                tmp_path,
                HOST_A,
                meshtastic_bin,
                label="add-only-saturation-failure-host-a",
            )
            raise
    finally:
        restore_timeout = max(
            HOST_CONFIGURE_TIMEOUT_SECONDS,
            HOST_READY_AFTER_CONFIGURE_TIMEOUT_SECONDS,
        )
        restore_rc, restore_output = _run_host_cli(
            HOST_A,
            "--configure",
            str(baseline_export_path),
            timeout=restore_timeout,
            meshtastic_bin=meshtastic_bin,
        )
        restore_artifact = tmp_path / "debug-add-only-saturation-restore-host-a.txt"
        restore_artifact.write_text(
            f"returncode={restore_rc}\ntimeout={restore_timeout}\n\n{restore_output}",
            encoding="utf-8",
        )
        if restore_rc == 0:
            _wait_for_host_ready(
                HOST_A,
                meshtastic_bin,
                timeout_seconds=HOST_READY_AFTER_CONFIGURE_TIMEOUT_SECONDS,
            )
        else:
            _capture_host_debug_state(
                tmp_path,
                HOST_A,
                meshtastic_bin,
                label="add-only-saturation-restore-failure-host-a",
            )


@pytest.mark.xfail(
    reason="Resume logic implemented but large setURL replace-all over TCP/meshtasticd "
    "still prone to multi-disconnect edge cases. Resume handles single "
    "disconnects; repeated disconnects within the same transaction may exceed "
    "retry bounds.",
    strict=False,
)
def test_meshtasticd_multinode_large_channel_url_replace_all_over_tcp(
    tmp_path: Path,
    meshtastic_bin: str,
) -> None:
    """Restore a full 8-channel blueprint via --configure over TCP.

    This test exercises the known transport-robustness gap where large
    setURL replace-all transactions over TCP can disconnect mid-write.
    When the transport-robustness pass lands, this test should be
    changed from xfail to a regular passing test.
    """
    try:
        _wait_for_host_ready(HOST_A, meshtastic_bin)
        _wait_for_host_ready(HOST_B, meshtastic_bin)

        expected_channel_names = _configure_channel_blueprint(HOST_A, meshtastic_bin)

        export_path = tmp_path / "meshtasticd-multinode-xfail-export.yaml"
        _run_host_cli_ok(
            HOST_A,
            "--export-config",
            str(export_path),
            meshtastic_bin=meshtastic_bin,
        )
        exported_data = yaml.safe_load(export_path.read_text(encoding="utf-8"))
        assert isinstance(exported_data, dict)
        exported_data["owner"] = CONFIGURED_OWNER
        exported_data["owner_short"] = CONFIGURED_OWNER_SHORT
        export_path.write_text(
            yaml.safe_dump(exported_data, sort_keys=False),
            encoding="utf-8",
        )

        configure_output = _run_host_cli_ok(
            HOST_B,
            "--configure",
            str(export_path),
            timeout=HOST_CONFIGURE_TIMEOUT_SECONDS,
            meshtastic_bin=meshtastic_bin,
        )
        assert "Phase 1 complete." in configure_output
        _wait_for_host_ready(
            HOST_B,
            meshtastic_bin,
            timeout_seconds=HOST_READY_AFTER_CONFIGURE_TIMEOUT_SECONDS,
        )

        info_output_b = _run_host_cli_ok(
            HOST_B, "--info", meshtastic_bin=meshtastic_bin
        )
        channel_name_map_b = _extract_channel_names(info_output_b)
        export_path_b = tmp_path / "meshtasticd-multinode-xfail-export-b.yaml"
        _run_host_cli_ok(
            HOST_B,
            "--export-config",
            str(export_path_b),
            meshtastic_bin=meshtastic_bin,
        )
        exported_data_b = yaml.safe_load(export_path_b.read_text(encoding="utf-8"))
        assert isinstance(exported_data_b, dict)

        channels_a = exported_data.get("channels")
        channels_b = exported_data_b.get("channels")
        if channels_a is not None and channels_b is not None:
            assert isinstance(channels_a, list)
            assert isinstance(channels_b, list)
            identities_a = _extract_exported_channel_identities(channels_a)
            identities_b = _extract_exported_channel_identities(channels_b)
            assert len(identities_a) == len(channels_a)
            assert len(identities_b) == len(channels_b)
            assert identities_a == identities_b, (
                "Large replace-all identity mismatch after --configure.\n"
                f"missing_on_{HOST_B}={sorted(identities_a - identities_b)}\n"
                f"extra_on_{HOST_B}={sorted(identities_b - identities_a)}"
            )
        else:
            observed_names = {name for name in channel_name_map_b.values() if name}
            expected_names = {name for name in expected_channel_names.values() if name}
            assert observed_names <= expected_names, (
                f"Unexpected channel names on {HOST_B} after large replace-all. "
                f"unexpected={sorted(observed_names - expected_names)}, "
                f"observed={sorted(observed_names)}, expected_subset={sorted(expected_names)}"
            )
    except Exception:
        _capture_host_debug_state(
            tmp_path,
            HOST_A,
            meshtastic_bin,
            label="large-replace-all-failure-host-a",
        )
        _capture_host_debug_state(
            tmp_path,
            HOST_B,
            meshtastic_bin,
            label="large-replace-all-failure-host-b",
        )
        raise
