"""Tests for Arrow/Feather slog writer helpers."""

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    import pyarrow as pa
    from pyarrow import feather
else:
    pa = pytest.importorskip("pyarrow")
    from pyarrow import feather  # noqa: E402

import meshtastic.slog.arrow as arrow_module
from meshtastic.slog.arrow import FeatherWriter


def _test_schema() -> "pa.Schema":
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


@pytest.mark.unit
def test_arrow_writer_set_schema_alias_delegates_to_internal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """set_schema should forward directly to _set_schema."""
    writer = arrow_module.ArrowWriter(str(tmp_path / "set-schema-alias.arrow"))
    try:
        schema = _test_schema()
        set_schema_mock = MagicMock()
        monkeypatch.setattr(writer, "_set_schema", set_schema_mock)

        writer.set_schema(schema)

        set_schema_mock.assert_called_once_with(schema)
    finally:
        writer.close()


@pytest.mark.unit
def test_arrow_writer_add_row_raises_when_closed(tmp_path: Path) -> None:
    """_add_row should reject writes after the writer is marked closed."""
    writer = arrow_module.ArrowWriter(str(tmp_path / "closed-add-row.arrow"))
    writer._closed = True
    try:
        with pytest.raises(arrow_module.WriterClosedError):
            writer._add_row({"value": 1})
    finally:
        writer.sink.close()


@pytest.mark.unit
def test_arrow_writer_add_row_flushes_when_chunk_size_reached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_add_row should trigger _write once buffered rows reach CHUNK_SIZE."""
    writer = arrow_module.ArrowWriter(str(tmp_path / "chunk-flush.arrow"))
    try:
        writer.new_rows = [{"value": i} for i in range(arrow_module.CHUNK_SIZE - 1)]
        write_mock = MagicMock()
        monkeypatch.setattr(writer, "_write", write_mock)

        writer._add_row({"value": arrow_module.CHUNK_SIZE})

        write_mock.assert_called_once_with()
    finally:
        writer.sink.close()


@pytest.mark.unit
def test_arrow_writer_add_row_alias_delegates_to_internal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """add_row should delegate to _add_row for compatibility."""
    writer = arrow_module.ArrowWriter(str(tmp_path / "add-row-alias.arrow"))
    try:
        add_row_mock = MagicMock()
        monkeypatch.setattr(writer, "_add_row", add_row_mock)

        writer.add_row({"value": 1})

        add_row_mock.assert_called_once_with({"value": 1})
    finally:
        writer.close()


@pytest.mark.unit
def test_feather_writer_remove_stale_dest_logs_when_remove_fails(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """_remove_stale_dest_file should warn when unlinking an existing file fails."""
    dest_path = tmp_path / "stale.feather"
    dest_path.write_bytes(b"stale")

    with caplog.at_level(logging.WARNING):
        with patch("meshtastic.slog.arrow.os.remove", side_effect=OSError("busy")):
            FeatherWriter._remove_stale_dest_file(str(dest_path))

    assert "Failed to remove stale Feather destination file" in caplog.text


@pytest.mark.unit
def test_feather_writer_discard_empty_source_logs_remove_failure(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """_discard_empty_source should warn when deleting the empty source file fails."""
    src_path = tmp_path / "empty.arrow"
    src_path.write_bytes(b"")
    writer = object.__new__(FeatherWriter)
    writer._remove_stale_dest_file = MagicMock()  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        with patch("meshtastic.slog.arrow.os.remove", side_effect=OSError("busy")):
            writer._discard_empty_source(
                str(src_path),
                str(tmp_path / "dest.feather"),
                arrow_module.DISCARD_EMPTY_FILE_MESSAGE,
            )

    assert "Failed to remove empty Arrow source file" in caplog.text
    writer._remove_stale_dest_file.assert_called_once_with(
        str(tmp_path / "dest.feather")
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("conversion_done", "conversion_in_progress"),
    [(True, False), (False, True)],
)
def test_feather_writer_close_returns_early_when_conversion_already_set(
    conversion_done: bool,
    conversion_in_progress: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close should return early when conversion is already done or in-progress."""
    writer = object.__new__(FeatherWriter)
    writer._lock = threading.RLock()
    writer._conversion_done = conversion_done
    writer._conversion_in_progress = conversion_in_progress
    writer.base_file_name = "unused"
    super_close_mock = MagicMock()
    monkeypatch.setattr(arrow_module.ArrowWriter, "close", super_close_mock)

    writer.close()

    super_close_mock.assert_not_called()


@pytest.mark.unit
def test_feather_writer_close_treats_missing_source_as_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Close should remove stale destination and finish when source file is missing."""
    writer = object.__new__(FeatherWriter)
    writer._lock = threading.RLock()
    writer._conversion_done = False
    writer._conversion_in_progress = False
    writer.base_file_name = str(tmp_path / "missing-source")
    writer._remove_stale_dest_file = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setattr(arrow_module.ArrowWriter, "close", lambda _self: None)
    monkeypatch.setattr(arrow_module.os.path, "exists", lambda _path: False)  # type: ignore[attr-defined]

    writer.close()

    writer._remove_stale_dest_file.assert_called_once_with(
        str(tmp_path / "missing-source.feather")
    )
    assert writer._conversion_done is True
    assert writer._conversion_in_progress is False


@pytest.mark.unit
def test_feather_writer_close_discards_zero_size_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Close should route zero-size Arrow sources through discard helper."""
    writer = object.__new__(FeatherWriter)
    writer._lock = threading.RLock()
    writer._conversion_done = False
    writer._conversion_in_progress = False
    writer.base_file_name = str(tmp_path / "zero-size")
    writer._discard_empty_source = MagicMock()  # type: ignore[method-assign]

    monkeypatch.setattr(arrow_module.ArrowWriter, "close", lambda _self: None)
    monkeypatch.setattr(arrow_module.os.path, "exists", lambda _path: True)  # type: ignore[attr-defined]
    monkeypatch.setattr(arrow_module.os.path, "getsize", lambda _path: 0)  # type: ignore[attr-defined]

    writer.close()

    writer._discard_empty_source.assert_called_once_with(
        str(tmp_path / "zero-size.arrow"),
        str(tmp_path / "zero-size.feather"),
        arrow_module.DISCARD_EMPTY_FILE_MESSAGE,
    )
    assert writer._conversion_done is True
    assert writer._conversion_in_progress is False


@pytest.mark.unit
def test_feather_writer_close_logs_when_temp_remove_fails_for_empty_batches(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Close should warn when temp Feather cleanup fails in empty-batch conversion path."""
    base_path = tmp_path / "empty-batches"
    writer = FeatherWriter(str(base_path))
    writer.setSchema(_test_schema())
    original_remove = os.remove

    def _remove_with_temp_failure(path: str) -> None:
        if str(path).endswith(arrow_module.TEMP_FEATHER_SUFFIX):
            raise OSError("temp cleanup failed")
        original_remove(path)

    with caplog.at_level(logging.WARNING):
        with patch(
            "meshtastic.slog.arrow.os.remove", side_effect=_remove_with_temp_failure
        ):
            writer.close()

    assert "Failed to remove temporary Feather file" in caplog.text


@pytest.mark.unit
def test_feather_writer_close_logs_when_temp_remove_fails_after_conversion_exception(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Close should warn if temp Feather file cannot be removed after conversion failure."""
    base_path = tmp_path / "conversion-exception"
    writer = FeatherWriter(str(base_path))
    writer.setSchema(_test_schema())
    writer.addRow({"value": 1})
    original_remove = os.remove

    def _remove_with_temp_failure(path: str) -> None:
        if str(path).endswith(arrow_module.TEMP_FEATHER_SUFFIX):
            raise OSError("temp cleanup failed")
        original_remove(path)

    with caplog.at_level(logging.WARNING):
        with (
            patch(
                "meshtastic.slog.arrow.pa.memory_map",
                side_effect=RuntimeError("conversion failed"),
            ),
            patch(
                "meshtastic.slog.arrow.os.remove", side_effect=_remove_with_temp_failure
            ),
            pytest.raises(RuntimeError, match="conversion failed"),
        ):
            writer.close()

    assert "Failed to remove temporary Feather file" in caplog.text


@pytest.mark.unit
def test_feather_writer_close_logs_when_source_remove_fails_after_success(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Close should warn and continue if temporary Arrow source removal fails."""
    base_path = tmp_path / "source-remove-fail"
    writer = FeatherWriter(str(base_path))
    writer.setSchema(_test_schema())
    writer.addRow({"value": 1})
    src_path = str(base_path) + arrow_module.ARROW_EXTENSION
    original_remove = os.remove

    def _remove_with_source_failure(path: str) -> None:
        if str(path) == src_path:
            raise OSError("cannot remove source")
        original_remove(path)

    with caplog.at_level(logging.WARNING):
        with patch(
            "meshtastic.slog.arrow.os.remove", side_effect=_remove_with_source_failure
        ):
            writer.close()

    assert "Failed to remove temporary Arrow source file" in caplog.text
    assert (tmp_path / "source-remove-fail.feather").exists()
