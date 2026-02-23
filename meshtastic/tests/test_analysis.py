"""Test analysis processing."""

import logging
import os
import sys
from typing import NoReturn

import pandas as pd
import pytest

try:
    # Depends upon matplotlib & other packages in poetry's analysis group, not installed by default
    from meshtastic.analysis import __main__ as analysis_main
    from meshtastic.analysis.__main__ import choose_power_column, main
except ImportError:
    pytest.skip("Can't import meshtastic.analysis", allow_module_level=True)


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
def test_main_routes_load_errors_through_our_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() should emit a graceful CLI error when slog loading fails."""
    captured: dict[str, str] = {}

    def _fake_create_dash(*, slog_path: str) -> None:
        _ = slog_path
        raise ValueError("bad slog")  # noqa: TRY003

    def _fake_our_exit(message: str, return_value: int = 1) -> NoReturn:
        _ = return_value
        captured["message"] = message
        raise SystemExit(1)

    monkeypatch.setattr(analysis_main, "create_dash", _fake_create_dash)
    monkeypatch.setattr(analysis_main, "our_exit", _fake_our_exit)
    monkeypatch.setattr(
        sys, "argv", ["fakescriptname", "--no-server", "--slog", os.devnull]
    )

    with pytest.raises(SystemExit):
        main()

    assert "Error loading slog data: bad slog" in captured["message"]
