"""Additional test coverage for meshtastic/analysis/__main__.py.

This module targets the uncovered lines identified in coverage analysis:
- Lines 40, 195, 202-203, 231-234, 272, 275, 288, 293, 302-303, 446-510, 541, 557-561
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

import pytest

OPTIONAL_ANALYSIS_DEPS = {
    "dash",
    "dash_bootstrap_components",
    "matplotlib",
    "numpy",
    "pandas",
    "plotly",
    "pyarrow",
}

try:
    import numpy as np
    import pandas as pd
    from pyarrow import feather

    from meshtastic.analysis import __main__ as analysis_main
    from meshtastic.analysis.__main__ import (
        _cli_exit,
        create_argparser,
        create_dash,
        get_board_info,
        get_pmon_raises,
        main,
    )
except ModuleNotFoundError as exc:
    missing = (exc.name or "").split(".", maxsplit=1)[0]
    if missing in OPTIONAL_ANALYSIS_DEPS:
        pytest.skip("Can't import meshtastic.analysis", allow_module_level=True)
    raise


@pytest.mark.unit
def test_cli_exit_delegates_to_util_our_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_cli_exit should call util.our_exit with the provided message and return code (line 40)."""
    captured: dict[str, Any] = {}

    def _fake_our_exit(message: str, return_code: int = 1) -> NoReturn:
        captured["message"] = message
        captured["code"] = return_code
        raise SystemExit(return_code)

    monkeypatch.setattr(analysis_main.util, "our_exit", _fake_our_exit)

    with pytest.raises(SystemExit) as exc_info:
        _cli_exit("test error", 42)

    assert captured["message"] == "test error"
    assert captured["code"] == 42
    assert exc_info.value.code == 42


@pytest.mark.unit
def test_get_pmon_raises_requires_time_column() -> None:
    """get_pmon_raises should raise when time column is missing (line 195)."""
    dslog = pd.DataFrame(
        {
            "pm_mask": [1, 2, 3],
        }
    )
    with pytest.raises(ValueError, match="No time column found in slog"):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_non_numeric_pm_mask() -> None:
    """get_pmon_raises should raise when pm_mask contains non-numeric values (lines 202-203)."""
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0, 3.0],
            "pm_mask": ["not_a_number", "also_not", "invalid"],
        }
    )
    with pytest.raises(ValueError, match="pm_mask contains non-numeric values"):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_rejects_uint64_overflow_via_float_check() -> None:
    """get_pmon_raises should reject values exceeding uint64 range.

    Large integers are converted to float64 by pd.to_numeric, so they hit
    the float64 exactness check before reaching the uint64 range check.
    """
    uint64_max = np.iinfo(np.uint64).max
    overflow_val = int(uint64_max) + 1
    # Large values become float and trigger float64 exactness check
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": pd.array([0, overflow_val], dtype="object"),
        }
    )
    with pytest.raises(
        ValueError,
        match="pm_mask contains values that cannot be exactly represented in float64",
    ):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_board_info_requires_sw_version_column() -> None:
    """get_board_info should raise when sw_version column is missing (line 272)."""
    dslog = pd.DataFrame(
        {
            "board_id": [1, 2, 3],
        }
    )
    with pytest.raises(ValueError, match="No sw_version column found in dslog"):
        get_board_info(dslog)


@pytest.mark.unit
def test_get_board_info_requires_non_empty_board_info() -> None:
    """get_board_info should raise when board_info is empty (line 275)."""
    dslog = pd.DataFrame(
        {
            "sw_version": [None, None],
            "board_id": [1, 2],
        }
    )
    with pytest.raises(ValueError, match="No board info rows found in dslog"):
        get_board_info(dslog)


@pytest.mark.unit
def test_get_board_info_rejects_non_integral_float() -> None:
    """get_board_info should reject non-integral float board_id values (line 288)."""
    dslog = pd.DataFrame(
        {
            "sw_version": ["2.5.0"],
            "board_id": [1.5],  # Non-integral float
        }
    )
    with pytest.raises(ValueError, match="Invalid board_id value in dslog"):
        get_board_info(dslog)


@pytest.mark.unit
def test_get_board_info_rejects_invalid_string_board_id() -> None:
    """get_board_info should reject non-integer string board_id values (line 293)."""
    dslog = pd.DataFrame(
        {
            "sw_version": ["2.5.0"],
            "board_id": ["not_a_number"],  # Invalid string
        }
    )
    with pytest.raises(ValueError, match="Invalid board_id value in dslog"):
        get_board_info(dslog)


@pytest.mark.unit
def test_get_board_info_rejects_unknown_board_id() -> None:
    """get_board_info should raise for unknown board_id values (lines 302-303)."""
    # Use an impossibly large board ID that's definitely not in the enum
    unknown_board_id = 999999999
    dslog = pd.DataFrame(
        {
            "sw_version": ["2.5.0"],
            "board_id": [unknown_board_id],
        }
    )
    with pytest.raises(ValueError, match="Unknown board_id value in dslog"):
        get_board_info(dslog)


@pytest.mark.unit
def test_create_dash_returns_configured_app(tmp_path: Path) -> None:
    """create_dash should return a configured Dash application (lines 446-510).

    Creates a temporary slog directory with power.feather and slog.feather
    files to test the full Dash app creation flow.
    """
    # Create temporary test data
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    # Create power.feather with required columns
    power_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "average_mA": pd.array([100, 150, 200], dtype=pd.Float64Dtype()),
            "max_mA": pd.array([110, 160, 210], dtype=pd.Float64Dtype()),
            "min_mA": pd.array([90, 140, 190], dtype=pd.Float64Dtype()),
        }
    )
    feather.write_feather(power_data, str(slog_dir / "power.feather"))

    # Create slog.feather with required columns
    slog_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "pm_mask": pd.array([0, 0, 0], dtype=pd.Int64Dtype()),
        }
    )
    feather.write_feather(slog_data, str(slog_dir / "slog.feather"))

    # Create the Dash app
    app = create_dash(str(slog_dir))

    # Verify the app was created successfully
    assert app is not None
    assert hasattr(app, "layout")
    assert app.layout is not None
    # Layout should be a list with header and graph
    assert len(app.layout) == 2


@pytest.mark.unit
def test_create_dash_with_pmon_raises(tmp_path: Path) -> None:
    """create_dash should handle power monitor raise events (lines 446-510)."""
    from meshtastic import powermon_pb2

    # Find a valid power monitor state
    single_bits = sorted(
        state.number
        for state in powermon_pb2.PowerMon.State.DESCRIPTOR.values
        if state.number > 0 and (state.number & (state.number - 1)) == 0
    )
    if len(single_bits) < 1:
        pytest.skip("Need at least one single-bit power monitor state")

    # Create temporary test data with power monitor events
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    # Create power.feather
    power_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "average_mA": pd.array([100, 150, 200], dtype=pd.Float64Dtype()),
            "max_mA": pd.array([110, 160, 210], dtype=pd.Float64Dtype()),
            "min_mA": pd.array([90, 140, 190], dtype=pd.Float64Dtype()),
        }
    )
    feather.write_feather(power_data, str(slog_dir / "power.feather"))

    # Create slog.feather with power monitor raises
    slog_data = pd.DataFrame(
        {
            "time": pd.array([1.0, 2.0, 3.0]),
            "pm_mask": pd.array([0, single_bits[0], single_bits[0]], dtype="object"),
        }
    )
    feather.write_feather(slog_data, str(slog_dir / "slog.feather"))

    # Create the Dash app
    app = create_dash(str(slog_dir))

    # Verify the app was created with power monitor markers
    assert app is not None
    assert hasattr(app, "layout")
    assert app.layout is not None


@pytest.mark.unit
def test_create_dash_with_legacy_mW_columns(tmp_path: Path) -> None:
    """create_dash should work with legacy mW column names (lines 446-510)."""
    # Create temporary test data with legacy column names
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    # Create power.feather with legacy mW column names
    power_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "average_mW": pd.array([1000, 1500, 2000], dtype=pd.Float64Dtype()),
            "max_mW": pd.array([1100, 1600, 2100], dtype=pd.Float64Dtype()),
            "min_mW": pd.array([900, 1400, 1900], dtype=pd.Float64Dtype()),
        }
    )
    feather.write_feather(power_data, str(slog_dir / "power.feather"))

    # Create slog.feather
    slog_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "pm_mask": pd.array([0, 0, 0], dtype=pd.Int64Dtype()),
        }
    )
    feather.write_feather(slog_data, str(slog_dir / "slog.feather"))

    # Create the Dash app - should use legacy columns
    app = create_dash(str(slog_dir))

    # Verify the app was created successfully
    assert app is not None
    assert hasattr(app, "layout")


@pytest.mark.unit
def test_create_dash_fails_when_power_file_missing(tmp_path: Path) -> None:
    """create_dash should raise FileNotFoundError when power.feather is missing."""
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    # Only create slog.feather, not power.feather
    slog_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "pm_mask": pd.array([0, 0, 0], dtype=pd.Int64Dtype()),
        }
    )
    feather.write_feather(slog_data, str(slog_dir / "slog.feather"))

    with pytest.raises((FileNotFoundError, OSError)):
        create_dash(str(slog_dir))


@pytest.mark.unit
def test_main_uses_default_slog_path(
    monkeypatch: pytest.MonkeyPatch,
    cli_exit_capture: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """main() should use default slog path when --slog not provided (line 541)."""

    class _FakeApp:
        def run(self, *, debug: bool, host: str, port: int) -> None:
            _ = (debug, host, port)

    def _fake_create_dash(*, slog_path: str) -> _FakeApp:
        _ = slog_path
        return _FakeApp()

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(sys, "argv", ["fakescriptname", "--no-server"])
    monkeypatch.setattr(logging.getLogger(), "propagate", True)

    with caplog.at_level(logging.DEBUG):
        main()

    assert "Exiting without running visualization server" in caplog.text


@pytest.mark.unit
def test_main_disables_debug_for_non_loopback_host(
    monkeypatch: pytest.MonkeyPatch,
    cli_exit_capture: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """main() should disable debug mode when host is not loopback (lines 557-561)."""

    class _FakeApp:
        def run(self, *, debug: bool, host: str, port: int) -> None:
            _ = (debug, host, port)

    def _fake_create_dash(*, slog_path: str) -> _FakeApp:
        _ = slog_path
        return _FakeApp()

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakescriptname", "--slog", os.devnull, "--debug", "--host", "0.0.0.0"],
    )
    monkeypatch.setattr(logging.getLogger(), "propagate", True)

    with caplog.at_level(logging.DEBUG):
        main()

    assert "Ignoring --debug because host 0.0.0.0 is not localhost" in caplog.text


@pytest.mark.unit
def test_create_dash_fails_when_slog_file_missing(tmp_path: Path) -> None:
    """create_dash should raise FileNotFoundError when slog.feather is missing."""
    slog_dir = tmp_path / "test_slog"
    slog_dir.mkdir()

    # Only create power.feather, not slog.feather
    power_data = pd.DataFrame(
        {
            "time": pd.array([1, 2, 3], dtype=pd.Int64Dtype()),
            "average_mA": pd.array([100, 150, 200], dtype=pd.Float64Dtype()),
            "max_mA": pd.array([110, 160, 210], dtype=pd.Float64Dtype()),
            "min_mA": pd.array([90, 140, 190], dtype=pd.Float64Dtype()),
        }
    )
    feather.write_feather(power_data, str(slog_dir / "power.feather"))

    with pytest.raises((FileNotFoundError, OSError)):
        create_dash(str(slog_dir))


@pytest.mark.unit
def test_parse_port_rejects_non_integer() -> None:
    """_parse_port should raise ArgumentTypeError for non-integer input."""
    with pytest.raises(SystemExit):
        # SystemExit because argparse exits on error
        parser = create_argparser()
        parser.parse_args(["--port", "not-a-number"])


@pytest.mark.unit
def test_parse_port_rejects_out_of_range() -> None:
    """_parse_port should raise ArgumentTypeError for out-of-range ports."""
    with pytest.raises(SystemExit):
        parser = create_argparser()
        parser.parse_args(["--port", "70000"])

    with pytest.raises(SystemExit):
        parser = create_argparser()
        parser.parse_args(["--port", "0"])


@pytest.mark.unit
def test_parse_port_accepts_valid_ports() -> None:
    """_parse_port should accept valid port numbers."""
    parser = create_argparser()

    args = parser.parse_args(["--port", "8051"])
    assert args.port == 8051

    args = parser.parse_args(["--port", "1"])
    assert args.port == 1

    args = parser.parse_args(["--port", "65535"])
    assert args.port == 65535


@pytest.mark.unit
def test_get_pmon_raises_rejects_float_masks() -> None:
    """get_pmon_raises should reject float pm_mask values beyond 2**53."""
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0.0, float((1 << 53) + 2)],
        }
    )
    with pytest.raises(
        ValueError,
        match="pm_mask contains values that cannot be exactly represented in float64",
    ):
        get_pmon_raises(dslog)
