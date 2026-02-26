"""Utilities for Apache Arrow serialization."""

import logging
import os
import tempfile
import threading
import warnings

import pyarrow as pa
from pyarrow import feather

CHUNK_SIZE = 1000  # disk writes are batched based on this number of rows
logger = logging.getLogger(__name__)


class ArrowWriterStateError(RuntimeError):
    """Raised for internal ArrowWriter state or initialization errors."""


class SchemaAlreadySetError(ArrowWriterStateError):
    """Raised when set_schema() is called after a schema is already configured."""

    def __init__(self) -> None:
        super().__init__("Schema already set; cannot call set_schema more than once.")


class StreamInitError(ArrowWriterStateError):
    """Raised when Arrow stream writer initialization fails."""

    def __init__(self) -> None:
        super().__init__("Failed to initialize Arrow stream writer.")


class WriterNoneError(ArrowWriterStateError):
    """Raised when writer is unexpectedly None after schema initialization."""

    def __init__(self) -> None:
        super().__init__(
            "Writer is None despite schema being set; schema initialization may have failed."
        )


class WriterClosedError(ArrowWriterStateError):
    """Raised when an ArrowWriter operation is invoked after close()."""

    def __init__(self) -> None:
        super().__init__("Cannot perform operation: ArrowWriter is closed.")


class ArrowWriter:
    """Writes an arrow file in a streaming fashion."""

    def __init__(self, file_name: str) -> None:
        """Initialize an ArrowWriter that streams Arrow-formatted data to the given file.

        Opens a writable file sink, initializes the in-memory row buffer and
        schema/writer placeholders, and creates a re-entrant lock to guard
        concurrent access. The schema is not inferred or set until data is written
        or set_schema is called.

        Parameters
        ----------
        file_name : str
            Path to the output file to write Arrow stream data to.
        """
        self.sink = pa.OSFile(file_name, "wb")
        self.new_rows: list[dict[str, object]] = []
        self.schema: pa.Schema | None = None  # haven't yet learned the schema
        self.writer: pa.RecordBatchStreamWriter | None = None
        # Re-entrant: _write() can call set_schema() while the same lock is held.
        self._lock = threading.RLock()
        self._closed = False
        self._warned_set_schema_deprecation = False
        self._warned_add_row_deprecation = False

    def __enter__(self) -> "ArrowWriter":
        """Return self for context-manager usage."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Close the writer on context-manager exit and propagate exceptions.

        If an exception occurred within the with-block (exc_type is not None),
        any exception raised by close() is logged and suppressed so the original
        exception propagates. If no with-block exception occurred, close() exceptions
        are allowed to propagate normally.
        """
        _ = traceback
        try:
            self.close()
        except Exception:
            if exc_type is not None:
                logger.warning(
                    "close() failed while unwinding an existing exception.",
                    exc_info=True,
                )
            else:
                raise

    def close(self) -> None:
        """Close the writer, flush any buffered rows, and close the underlying sink.

        Flushes any accumulated rows to disk, closes the RecordBatchStreamWriter if one exists, and closes the file sink.
        """
        with self._lock:
            if self._closed:
                return
            write_exc: Exception | None = None
            cleanup_exc: Exception | None = None

            try:
                self._write()
            except Exception as exc:  # noqa: BLE001 - preserve primary write failure
                write_exc = exc

            try:
                if self.writer:
                    self.writer.close()
            except Exception as exc:  # noqa: BLE001 - cleanup should still continue
                cleanup_exc = exc
                logger.warning(
                    "Failed to close Arrow stream writer cleanly.",
                    exc_info=True,
                )

            try:
                self.sink.close()
            except Exception as exc:  # noqa: BLE001 - cleanup should still continue
                if cleanup_exc is None:
                    cleanup_exc = exc
                else:
                    logger.warning(
                        "Additional sink close failure while cleaning up Arrow writer.",
                        exc_info=True,
                    )
            finally:
                self._closed = True

            if write_exc is not None:
                if cleanup_exc is not None:
                    logger.warning(
                        "Suppressed close() cleanup failure after write error: %s",
                        cleanup_exc,
                    )
                raise write_exc
            if cleanup_exc is not None:
                raise cleanup_exc

    def _set_schema(self, schema: pa.Schema) -> None:
        """Set the schema for the file.

        Only needed for datasets where we can't learn it from the first record written.

        Parameters
        ----------
        schema : pa.Schema
            The schema to use for the Arrow file.
        """
        with self._lock:
            if self._closed:
                raise WriterClosedError()
            if self.schema is not None:
                raise SchemaAlreadySetError()
            try:
                writer = pa.ipc.new_stream(self.sink, schema)
            except Exception as exc:
                raise StreamInitError() from exc
            self.schema = schema
            self.writer = writer

    def set_schema(self, schema: pa.Schema) -> None:
        """Call `setSchema()` instead."""
        with self._lock:
            if not self._warned_set_schema_deprecation:
                warnings.warn(
                    "set_schema() is deprecated; use setSchema() instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                self._warned_set_schema_deprecation = True
        self._set_schema(schema)

    def setSchema(self, schema: pa.Schema) -> None:
        """Public camelCase wrapper for _set_schema()."""
        self._set_schema(schema)

    def _write(self) -> None:
        """Write buffered rows to the file.

        Must be called with ``self._lock`` held.
        """
        # Precondition (enforced by callers): self._lock must be held.
        if len(self.new_rows) > 0:
            if self.schema is None:
                # only need to look at the first row to learn the schema
                self._set_schema(pa.Table.from_pylist([self.new_rows[0]]).schema)

            if self.writer is None:
                raise WriterNoneError()
            self.writer.write_batch(
                pa.RecordBatch.from_pylist(self.new_rows, schema=self.schema)
            )
            self.new_rows = []

    def _add_row(self, row_dict: dict[str, object]) -> None:
        """Add a row to the arrow file.

        We will automatically learn the schema from the first row. But all rows must use that schema.

        Parameters
        ----------
        row_dict : dict[str, object]
            Dictionary representing a single row with field names matching the schema.
        """
        with self._lock:
            if self._closed:
                raise WriterClosedError()
            self.new_rows.append(row_dict)
            if len(self.new_rows) >= CHUNK_SIZE:
                self._write()

    def add_row(self, row_dict: dict[str, object]) -> None:
        """Call `addRow()` instead."""
        with self._lock:
            if not self._warned_add_row_deprecation:
                warnings.warn(
                    "add_row() is deprecated; use addRow() instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                self._warned_add_row_deprecation = True
        self._add_row(row_dict)

    def addRow(self, row_dict: dict[str, object]) -> None:
        """Public camelCase wrapper for _add_row()."""
        self._add_row(row_dict)


class FeatherWriter(ArrowWriter):
    """A smaller more interoperable version of arrow files.

    Uses a temporary .arrow file (which could be huge) but converts to a much smaller (but still fast)
    feather file.
    """

    def __init__(self, file_name: str) -> None:
        """Initialize a FeatherWriter that will write compressed Feather files.

        Creates an ArrowWriter for a temporary .arrow file that will later be
        converted to a compressed Feather file upon close.

        Parameters
        ----------
        file_name : str
            Base path for the output file (without extension).
        """
        super().__init__(file_name + ".arrow")
        self.base_file_name = file_name
        self._conversion_done = False

    def close(self) -> None:
        """Close the writer and convert the temporary Arrow file to Feather format.

        Converts the temporary .arrow file to a compressed .feather file and
        removes the temporary file. Empty Arrow files are discarded.
        """
        with self._lock:
            if self._conversion_done:
                return
            super().close()
            src_name = self.base_file_name + ".arrow"
            dest_name = self.base_file_name + ".feather"
            if not os.path.exists(src_name):
                self._conversion_done = True
                return
            if os.path.getsize(src_name) == 0:
                logger.warning("Discarding empty file: %s", src_name)
                try:
                    os.remove(src_name)
                except OSError:
                    logger.warning(
                        "Failed to remove empty Arrow source file %s",
                        src_name,
                        exc_info=True,
                    )
                self._conversion_done = True
                return

            logger.info("Compressing log data into %s", dest_name)

            # note: must use open_stream, not open_file/read_table because the streaming layout is different
            # data = feather.read_table(src_name)
            with pa.memory_map(src_name) as source:
                with pa.ipc.open_stream(source) as reader:
                    array = reader.read_all()

            # Check for zero-row streams and discard them
            if array.num_rows == 0:
                logger.warning("Discarding empty Arrow file: %s", src_name)
                try:
                    os.remove(src_name)
                except OSError:
                    logger.warning(
                        "Failed to remove empty Arrow source file %s",
                        src_name,
                        exc_info=True,
                    )
                self._conversion_done = True
                return

            # See https://stackoverflow.com/a/72406099 for more info and performance testing measurements
            temp_name: str | None = None
            try:
                # Reserve a unique temp path in the destination directory
                # (delete=False), then close it so write_feather can reopen
                # that path before os.replace() performs an atomic swap.
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=os.path.dirname(dest_name) or ".",
                    prefix=os.path.basename(dest_name) + ".",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_name = temp_file.name

                feather.write_feather(array, temp_name, compression="zstd")
                os.replace(temp_name, dest_name)
                temp_name = None
            except Exception:
                if temp_name and os.path.exists(temp_name):
                    try:
                        os.remove(temp_name)
                    except OSError:
                        logger.warning(
                            "Failed to remove temporary Feather file %s",
                            temp_name,
                            exc_info=True,
                        )
                raise
            try:
                os.remove(src_name)
            except OSError:
                logger.warning(
                    "Failed to remove temporary Arrow source file %s",
                    src_name,
                    exc_info=True,
                )
            self._conversion_done = True
