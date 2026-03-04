"""Tests for Arrow/Feather slog writer helpers."""

from pathlib import Path

import pyarrow as pa
import pytest
from pyarrow import feather

import meshtastic.slog.arrow as arrow_module
from meshtastic.slog.arrow import FeatherWriter


def _test_schema() -> pa.Schema:
    """Return a compact deterministic schema for FeatherWriter tests."""
    return pa.schema([pa.field("value", pa.int64())])


@pytest.mark.unit
def test_feather_writer_close_writes_rows_without_source_artifacts(
    tmp_path: Path,
) -> None:
    """close() should convert .arrow stream rows into a readable .feather file."""
    base_path = tmp_path / "power-log"
    writer = FeatherWriter(str(base_path))
    writer.setSchema(_test_schema())
    writer.addRow({"value": 1})
    writer.addRow({"value": 2})

    writer.close()

    src_path = tmp_path / "power-log.arrow"
    dest_path = tmp_path / "power-log.feather"
    assert not src_path.exists()
    assert dest_path.exists()
    table = feather.read_table(str(dest_path))
    assert table.column("value").to_pylist() == [1, 2]


@pytest.mark.unit
def test_feather_writer_close_discards_empty_stream_outputs(tmp_path: Path) -> None:
    """close() should drop zero-row stream outputs instead of persisting empty Feather files."""
    base_path = tmp_path / "empty-log"
    writer = FeatherWriter(str(base_path))
    writer.setSchema(_test_schema())

    writer.close()

    src_path = tmp_path / "empty-log.arrow"
    dest_path = tmp_path / "empty-log.feather"
    assert not src_path.exists()
    assert not dest_path.exists()


@pytest.mark.unit
def test_feather_writer_close_resets_in_progress_on_super_close_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """close() should clear in-progress state when ArrowWriter.close() fails."""

    def _raise_close_error(_self: arrow_module.ArrowWriter) -> None:
        raise RuntimeError("close failure")

    monkeypatch.setattr(arrow_module.ArrowWriter, "close", _raise_close_error)
    writer = FeatherWriter(str(tmp_path / "close-failure"))

    with pytest.raises(RuntimeError, match="close failure"):
        writer.close()

    assert writer._conversion_done is False
    assert writer._conversion_in_progress is False

    # A second close() should attempt conversion again rather than returning early.
    with pytest.raises(RuntimeError, match="close failure"):
        writer.close()
