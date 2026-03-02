"""Unit tests for power logging behavior in slog.py."""

import logging
import threading
import warnings
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

import meshtastic.slog as slog_package

try:
    from meshtastic.slog import slog as slog_module
    from meshtastic.slog.slog import (
        POWER_LOG_SCHEMA_METADATA,
        LogSet,
        PowerLogger,
        StructuredLogger,
        rootDir,
        root_dir,
    )
except ImportError:
    pytest.skip("Can't import meshtastic.slog", allow_module_level=True)


class _FakeThread:
    """Lightweight thread stub used to avoid background work in tests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs
        self.started = False

    def start(self) -> None:
        """Record that start() was called."""
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        """No-op join for compatibility with the production thread API."""

    def is_alive(self) -> bool:
        """Return False to match threading.Thread API."""
        return False


class _FakeWriter:
    """In-memory writer stub for schema assertions."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.schema: Any = None

    def set_schema(self, schema: Any) -> None:
        """Capture the schema configured by PowerLogger."""
        self.schema = schema

    def setSchema(self, schema: Any) -> None:
        """Primary camelCase API used by production code; delegates to set_schema for schema capture."""
        self.set_schema(schema)

    def close(self) -> None:
        """No-op close used by tests."""


@pytest.mark.unit
def test_power_logger_sets_schema_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """PowerLogger should stamp compatibility metadata on its writer schema."""
    captured: dict[str, _FakeWriter] = {}

    def _fake_writer_factory(file_path: str) -> _FakeWriter:
        writer = _FakeWriter(file_path)
        captured["writer"] = writer
        return writer

    monkeypatch.setattr("meshtastic.slog.slog.FeatherWriter", _fake_writer_factory)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = MagicMock()
    logger = PowerLogger(meter, "unused-path")
    writer = captured["writer"]
    assert writer.schema is not None
    assert writer.schema.metadata == POWER_LOG_SCHEMA_METADATA
    assert writer.schema.names == [
        "time",
        "average_mA",
        "max_mA",
        "min_mA",
        "average_mW",
        "max_mW",
        "min_mW",
    ]
    logger.close()


@pytest.mark.unit
def test_power_logger_accepts_legacy_pmeter_constructor_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy `pMeter=` constructor keyword should remain accepted for compatibility."""
    monkeypatch.setattr("meshtastic.slog.slog.FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = MagicMock()
    logger = PowerLogger(pMeter=meter, file_path="unused-path")
    assert logger.pMeter is meter
    logger.close()


@pytest.mark.unit
def test_root_dir_legacy_alias_warns_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Legacy root_dir() alias should warn once and return the same path as rootDir()."""
    monkeypatch.setattr(
        slog_module.platformdirs, "user_data_dir", lambda *_args, **_kwargs: str(tmp_path)
    )
    slog_module._warned_deprecations.clear()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        legacy_path_first = root_dir()
        legacy_path_second = root_dir()
    preferred_path = rootDir()

    assert legacy_path_first == preferred_path
    assert legacy_path_second == preferred_path
    deprecations = [
        warning
        for warning in caught
        if issubclass(warning.category, DeprecationWarning)
    ]
    assert len(deprecations) == 1
    assert "root_dir()" in str(deprecations[0].message)


@pytest.mark.unit
def test_slog_public_exports_remain_available() -> None:
    """Slog package should keep expected historical public exports."""
    expected_exports = {"LogSet", "rootDir", "root_dir"}
    assert expected_exports.issubset(set(slog_package.__all__))
    for export_name in expected_exports:
        assert hasattr(slog_package, export_name)


@pytest.mark.unit
@pytest.mark.parametrize("interval", [0, -0.001, -1.0])
def test_power_logger_rejects_non_positive_interval(
    interval: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PowerLogger should reject interval <= 0 to prevent hot-loop threads."""
    monkeypatch.setattr("meshtastic.slog.slog.FeatherWriter", _FakeWriter)
    monkeypatch.setattr("meshtastic.slog.slog.threading.Thread", _FakeThread)

    meter = MagicMock()
    with pytest.raises(ValueError, match="interval must be > 0 seconds"):
        PowerLogger(meter, "unused-path", interval=interval)


@pytest.mark.unit
def test_store_current_reading_converts_legacy_aliases_when_voltage_present() -> None:
    """StoreCurrentReading should convert legacy *_mW aliases using nominal voltage."""
    meter = MagicMock()
    meter.v = 3.3
    meter.getAverageCurrentMA.return_value = 10.0
    meter.getMaxCurrentMA.return_value = 20.0
    meter.getMinCurrentMA.return_value = 5.0
    writer = MagicMock()

    power_logger = object.__new__(PowerLogger)
    power_logger.pMeter = meter
    power_logger.writer = writer
    power_logger._warned_legacy_mw_without_voltage = False
    power_logger._warned_store_current_reading_deprecation = False
    power_logger._reading_lock = threading.Lock()

    now = datetime(2026, 1, 1, 12, 0, 0)
    power_logger.storeCurrentReading(now=now)

    call = writer.addRow.call_args
    assert call is not None
    row = call.kwargs.get("row")
    if row is None:
        assert call.args, "Expected addRow to include row payload"
        row = call.args[0]
    assert row["time"] == now
    assert row["average_mA"] == 10.0
    assert row["max_mA"] == 20.0
    assert row["min_mA"] == 5.0
    assert row["average_mW"] == pytest.approx(33.0)
    assert row["max_mW"] == pytest.approx(66.0)
    assert row["min_mW"] == pytest.approx(16.5)
    meter.resetMeasurements.assert_called_once()


@pytest.mark.unit
def test_store_current_reading_warns_once_when_voltage_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """store_current_reading should preserve legacy behavior and warn once without voltage."""
    meter = MagicMock()
    meter.v = 0.0
    meter.getAverageCurrentMA.return_value = 11.0
    meter.getMaxCurrentMA.return_value = 21.0
    meter.getMinCurrentMA.return_value = 6.0
    writer = MagicMock()

    power_logger = object.__new__(PowerLogger)
    power_logger.pMeter = meter
    power_logger.writer = writer
    power_logger._warned_legacy_mw_without_voltage = False
    power_logger._warned_store_current_reading_deprecation = False
    power_logger._reading_lock = threading.Lock()

    with caplog.at_level(logging.WARNING):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            power_logger.store_current_reading(now=datetime(2026, 1, 1, 12, 0, 0))
            power_logger.store_current_reading(now=datetime(2026, 1, 1, 12, 0, 1))
        deprecations = [
            warning
            for warning in caught
            if issubclass(warning.category, DeprecationWarning)
        ]
        assert len(deprecations) == 1
        assert "store_current_reading()" in str(deprecations[0].message)

    rows = []
    for call in writer.addRow.call_args_list:
        row = call.kwargs.get("row")
        if row is None:
            assert call.args, "Expected addRow to include row payload"
            row = call.args[0]
        rows.append(row)
    assert len(rows) == 2
    for row in rows:
        assert row["average_mW"] == row["average_mA"]
        assert row["max_mW"] == row["max_mA"]
        assert row["min_mW"] == row["min_mA"]
    assert (
        caplog.text.count("Power meter does not expose nominal voltage") == 1
    ), caplog.text


@pytest.mark.unit
def test_on_log_message_keeps_raw_and_power_on_add_row_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """StructuredLogger should preserve raw/power side-effects when addRow fails."""
    logger = object.__new__(StructuredLogger)
    logger.writer = MagicMock()
    logger.writer.addRow.side_effect = RuntimeError("writer down")
    logger.power_logger = MagicMock()
    logger.include_raw = True
    raw_file = MagicMock()
    logger.raw_file = raw_file
    logger._raw_file_lock = threading.Lock()

    line = "prefix S:B:1,2.5.0"
    with caplog.at_level(logging.WARNING):
        logger._on_log_message(line)

    assert "Failed to write structured slog row" in caplog.text
    raw_file.write.assert_called_once_with(f"{line}\n")
    logger.power_logger.storeCurrentReading.assert_called_once()


@pytest.mark.unit
def test_on_log_message_legacy_alias_delegates() -> None:
    """Legacy internal `_onLogMessage` alias should delegate to `_on_log_message`."""
    logger = object.__new__(StructuredLogger)
    logger._on_log_message = MagicMock()  # type: ignore[method-assign]

    logger._onLogMessage("sample line")

    logger._on_log_message.assert_called_once_with("sample line")


@pytest.mark.unit
def test_log_set_close_preserves_primary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LogSet.close should preserve slog close failure and chain power close as cause."""
    log_set = object.__new__(LogSet)
    log_set.dir_name = "tmp"
    log_set.atexit_handler = lambda: None
    log_set.slog_logger = MagicMock()
    log_set.power_logger = MagicMock()

    slog_error = ValueError("slog close failed")
    power_error = RuntimeError("power close failed")
    log_set.slog_logger.close.side_effect = slog_error
    log_set.power_logger.close.side_effect = power_error
    monkeypatch.setattr("meshtastic.slog.slog.atexit.unregister", lambda _: None)

    with pytest.raises(ValueError, match="slog close failed") as exc_info:
        log_set.close()

    assert exc_info.value.__cause__ is power_error
    assert log_set.slog_logger is None
    assert log_set.power_logger is None
