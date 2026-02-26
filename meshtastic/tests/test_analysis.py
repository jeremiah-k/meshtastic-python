"""Test analysis processing."""

import logging
import os
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

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
    import pandas as pd
    import pyarrow as pa
    from pyarrow import feather

    # Depends upon matplotlib & other packages in poetry's analysis group, not installed by default
    from meshtastic import powermon_pb2
    from meshtastic.analysis import __main__ as analysis_main
    from meshtastic.analysis.__main__ import (
        choose_power_column,
        create_argparser,
        get_board_info,
        get_pmon_raises,
        main,
        read_pandas,
        to_pmon_names,
    )

    # Import private function for testing
    _is_loopback_host = analysis_main._is_loopback_host
except ModuleNotFoundError as exc:
    missing = (exc.name or "").split(".")[0]
    if missing in OPTIONAL_ANALYSIS_DEPS:
        pytest.skip("Can't import meshtastic.analysis", allow_module_level=True)
    raise


@pytest.mark.unit
def test_analysis(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test analysis processing."""

    cur_dir = os.path.dirname(os.path.abspath(__file__))
    slog_input_dir = os.path.join(cur_dir, "slog-test-input")

    monkeypatch.setattr(
        sys, "argv", ["fakescriptname", "--no-server", "--slog", slog_input_dir]
    )

    with caplog.at_level(logging.DEBUG):
        logging.getLogger().propagate = True  # Let our testing framework see our logs
        main()

    assert "Exiting without running visualization server" in caplog.text


@pytest.mark.unit
def test_choose_power_column_falls_back_to_new_when_legacy_all_null() -> None:
    """choose_power_column should use corrected column when legacy values are all null."""
    frame = pd.DataFrame({"average_mW": [None, None], "average_mA": [1.2, 1.5]})
    assert choose_power_column(frame, "average_mW", "average_mA") == "average_mA"


@pytest.mark.unit
def test_main_routes_load_errors_through_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() should emit a graceful CLI error when slog loading fails."""
    captured: dict[str, str] = {}

    def _fake_create_dash(*, slog_path: str) -> NoReturn:
        _ = slog_path
        raise ValueError("bad slog")  # noqa: TRY003

    def _fake_cli_exit(message: str, return_value: int = 1) -> NoReturn:
        _ = return_value
        captured["message"] = message
        raise SystemExit(1)

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(analysis_main, "_cli_exit", _fake_cli_exit)
    monkeypatch.setattr(
        sys, "argv", ["fakescriptname", "--no-server", "--slog", os.devnull]
    )

    with pytest.raises(SystemExit):
        main()

    assert "Error loading slog data: bad slog" in captured["message"]


@pytest.mark.unit
def test_get_board_info_requires_board_id_column() -> None:
    """get_board_info should fail with a clear error when board_id column is missing."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"]})
    with pytest.raises(ValueError, match="No board_id rows found in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_get_board_info_requires_non_null_board_id() -> None:
    """get_board_info should fail with a clear error when board_id is all null."""
    frame = pd.DataFrame({"sw_version": ["2.5.0"], "board_id": [None]})
    with pytest.raises(ValueError, match="No board_id rows found in dslog"):
        get_board_info(frame)


@pytest.mark.unit
def test_to_pmon_names_maps_valid_states() -> None:
    """to_pmon_names should convert valid power-monitor state integers to name strings."""
    result = to_pmon_names([1, 2, 3])
    assert isinstance(result, list)
    assert len(result) == 3
    assert result[0] == powermon_pb2.PowerMon.State.Name(cast(Any, 1))
    assert result[1] == powermon_pb2.PowerMon.State.Name(cast(Any, 2))
    # Value 3 is a combined bitmask (1|2) and should decode both bits.
    assert result[2] == (
        f"{powermon_pb2.PowerMon.State.Name(cast(Any, 1))}"
        f"|{powermon_pb2.PowerMon.State.Name(cast(Any, 2))}"
    )


@pytest.mark.unit
def test_to_pmon_names_handles_invalid_values() -> None:
    """to_pmon_names should return None for invalid state values."""
    known_values = {
        state.number for state in powermon_pb2.PowerMon.State.DESCRIPTOR.values  # type: ignore[attr-defined]
    }
    invalid_value = next(
        (1 << i for i in range(63) if (1 << i) not in known_values),
        0xFFFFFFFF,
    )
    assert invalid_value not in known_values
    result = to_pmon_names([invalid_value, -1, "invalid"])
    assert result == [None, None, None]


@pytest.mark.unit
def test_to_pmon_names_handles_none_state() -> None:
    """to_pmon_names should return None when state name is 'None'."""
    # State 0 typically maps to 'None' in the enum
    result = to_pmon_names([0])
    assert result == [None]


@pytest.mark.unit
def test_read_pandas_preserves_nullable_dtypes(tmp_path: Path) -> None:
    """read_pandas should map Arrow types to pandas nullable dtypes."""
    # Create a test DataFrame with various types
    test_data = pd.DataFrame(
        {
            "int_col": pd.array([1, 2, None], dtype=pd.Int32Dtype()),
            "bool_col": pd.array([True, False, None], dtype=pd.BooleanDtype()),
            "str_col": pd.array(["a", "b", None], dtype=pd.ArrowDtype(pa.string())),  # type: ignore[call-overload]
        }
    )

    # Write to feather
    test_file = tmp_path / "test.feather"
    feather.write_feather(test_data, str(test_file))

    # Read back
    result = read_pandas(str(test_file))

    # Check that nullable dtypes are preserved
    assert isinstance(result["int_col"].dtype, pd.Int32Dtype)
    assert isinstance(result["bool_col"].dtype, pd.BooleanDtype)
    assert pd.api.types.is_string_dtype(result["str_col"])


@pytest.mark.unit
def test_get_pmon_raises_extracts_raise_events() -> None:
    """get_pmon_raises should extract power-monitor raise events from slog."""
    # Create test data with pm_mask transitions
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0, 3.0, 4.0],
            "pm_mask": [0, 1, 3, 1],  # 0->1 (raise), 1->3 (raise), 3->1 (fall)
        }
    )

    result = get_pmon_raises(dslog)

    # Should have time and pm_raises columns
    assert "time" in result.columns
    assert "pm_raises" in result.columns
    # Should only include rows with raises (not falls): 0->1 and 1->3
    assert len(result) == 2
    assert result["time"].tolist() == [2.0, 3.0]
    assert result["pm_raises"].tolist() == [
        powermon_pb2.PowerMon.State.Name(cast(Any, 1)),
        powermon_pb2.PowerMon.State.Name(cast(Any, 2)),
    ]


@pytest.mark.unit
def test_get_pmon_raises_requires_time_column() -> None:
    """get_pmon_raises should fail clearly when the time column is missing."""
    dslog = pd.DataFrame({"pm_mask": [0, 1, 3]})
    with pytest.raises(ValueError, match="No time column found in slog"):
        get_pmon_raises(dslog)


@pytest.mark.unit
def test_get_pmon_raises_handles_multibit_raise_mask() -> None:
    """get_pmon_raises should decode simultaneous multi-bit raise masks."""
    dslog = pd.DataFrame(
        {
            "time": [1.0, 2.0],
            "pm_mask": [0, 3],  # 0->3 is a simultaneous two-bit raise
        }
    )

    result = get_pmon_raises(dslog)

    assert len(result) == 1
    assert result["time"].tolist() == [2.0]
    assert result["pm_raises"].tolist() == [
        (
            f"{powermon_pb2.PowerMon.State.Name(cast(Any, 1))}"
            f"|{powermon_pb2.PowerMon.State.Name(cast(Any, 2))}"
        )
    ]


@pytest.mark.unit
def test_is_loopback_host_recognizes_localhost() -> None:
    """_is_loopback_host should recognize common loopback addresses."""
    assert _is_loopback_host("localhost")
    assert _is_loopback_host("127.0.0.1")
    assert _is_loopback_host("::1")
    assert _is_loopback_host("[::1]")
    assert not _is_loopback_host("192.168.1.1")
    assert not _is_loopback_host("0.0.0.0")  # noqa: S104
    assert not _is_loopback_host("example.com")


@pytest.mark.unit
def test_is_loopback_host_handles_invalid_addresses() -> None:
    """_is_loopback_host should handle invalid addresses gracefully."""
    assert not _is_loopback_host("not-an-ip")
    assert not _is_loopback_host("")


@pytest.mark.unit
def test_create_argparser_returns_parser() -> None:
    """create_argparser should return an ArgumentParser with expected arguments."""
    parser = create_argparser()
    assert parser is not None

    # Test parsing with various arguments
    args = parser.parse_args(["--no-server"])
    assert args.no_server is True

    args = parser.parse_args(["--slog", "/some/path"])
    assert args.slog == "/some/path"

    args = parser.parse_args(["--port", "9000"])
    assert args.port == 9000

    args = parser.parse_args(["--bind-host", "0.0.0.0"])  # noqa: S104
    assert args.host == "0.0.0.0"  # noqa: S104


@pytest.mark.unit
def test_create_argparser_rejects_invalid_port() -> None:
    """create_argparser should reject ports outside the valid TCP range."""
    parser = create_argparser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "0"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "70000"])


@pytest.mark.unit
def test_choose_power_column_prefers_legacy_when_present() -> None:
    """choose_power_column should prefer legacy column when it has non-null values."""
    frame = pd.DataFrame({"average_mW": [1.0, 2.0], "average_mA": [3.0, 4.0]})
    assert choose_power_column(frame, "average_mW", "average_mA") == "average_mW"


@pytest.mark.unit
def test_choose_power_column_raises_when_neither_present() -> None:
    """choose_power_column should raise ValueError when neither column exists."""
    frame = pd.DataFrame({"other_col": [1.0, 2.0]})
    with pytest.raises(ValueError, match="Missing required power column"):
        choose_power_column(frame, "average_mW", "average_mA")


@pytest.mark.unit
def test_choose_power_column_uses_legacy_when_only_legacy_present() -> None:
    """choose_power_column should use legacy column even if all null when it's the only option."""
    frame = pd.DataFrame({"average_mW": [None, None]})
    assert choose_power_column(frame, "average_mW", "average_mA") == "average_mW"
