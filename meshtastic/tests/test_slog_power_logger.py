"""Unit tests for power logging behavior in slog.py."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

try:
    from meshtastic.slog.slog import POWER_LOG_SCHEMA_METADATA, PowerLogger
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
    logger.is_logging = False


@pytest.mark.unit
def test_store_current_reading_converts_legacy_aliases_when_voltage_present() -> None:
    """store_current_reading should convert legacy *_mW aliases using nominal voltage."""
    meter = MagicMock()
    meter.v = 3.3
    meter.get_average_current_mA.return_value = 10.0
    meter.get_max_current_mA.return_value = 20.0
    meter.get_min_current_mA.return_value = 5.0
    writer = MagicMock()

    power_logger = object.__new__(PowerLogger)
    power_logger.pMeter = meter
    power_logger.writer = writer
    power_logger._warned_legacy_mw_without_voltage = False
    power_logger._reading_lock = threading.Lock()

    now = datetime(2026, 1, 1, 12, 0, 0)
    power_logger.store_current_reading(now=now)

    row = writer.addRow.call_args.args[0]
    assert row["time"] == now
    assert row["average_mA"] == 10.0
    assert row["max_mA"] == 20.0
    assert row["min_mA"] == 5.0
    assert row["average_mW"] == pytest.approx(33.0)
    assert row["max_mW"] == pytest.approx(66.0)
    assert row["min_mW"] == pytest.approx(16.5)
    meter.reset_measurements.assert_called_once()


@pytest.mark.unit
def test_store_current_reading_warns_once_when_voltage_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """store_current_reading should preserve legacy behavior and warn once without voltage."""
    meter = MagicMock()
    meter.v = 0.0
    meter.get_average_current_mA.return_value = 11.0
    meter.get_max_current_mA.return_value = 21.0
    meter.get_min_current_mA.return_value = 6.0
    writer = MagicMock()

    power_logger = object.__new__(PowerLogger)
    power_logger.pMeter = meter
    power_logger.writer = writer
    power_logger._warned_legacy_mw_without_voltage = False
    power_logger._reading_lock = threading.Lock()

    with caplog.at_level(logging.WARNING):
        power_logger.store_current_reading(now=datetime(2026, 1, 1, 12, 0, 0))
        power_logger.store_current_reading(now=datetime(2026, 1, 1, 12, 0, 1))

    rows = [call.args[0] for call in writer.addRow.call_args_list]
    assert len(rows) == 2
    for row in rows:
        assert row["average_mW"] == row["average_mA"]
        assert row["max_mW"] == row["max_mA"]
        assert row["min_mW"] == row["min_mA"]
    assert (
        caplog.text.count("Power meter does not expose nominal voltage") == 1
    ), caplog.text
