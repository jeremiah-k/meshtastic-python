"""Utilities for Apache Arrow serialization."""

import logging
import os
import threading

import pyarrow as pa
from pyarrow import feather

CHUNK_SIZE = 1000  # disk writes are batched based on this number of rows


class ArrowWriter:
    """Writes an arrow file in a streaming fashion."""

    def __init__(self, file_name: str):
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
        self.sink = pa.OSFile(file_name, "wb")  # type: ignore
        self.new_rows: list[dict] = []
        self.schema: pa.Schema | None = None  # haven't yet learned the schema
        self.writer: pa.RecordBatchStreamWriter | None = None
        # Re-entrant: _write() can call set_schema() while the same lock is held.
        self._lock = threading.RLock()

    def close(self) -> None:
        """Close the writer, flush any buffered rows, and close the underlying sink.

        Flushes any accumulated rows to disk, closes the RecordBatchStreamWriter if one exists, and closes the file sink.
        """
        with self._lock:
            try:
                self._write()
            finally:
                try:
                    if self.writer:
                        self.writer.close()
                finally:
                    self.sink.close()

    def set_schema(self, schema: pa.Schema) -> None:
        """Set the schema for the file.

        Only needed for datasets where we can't learn it from the first record written.

        Parameters
        ----------
        schema : pa.Schema
            The schema to use for the Arrow file.
        """
        with self._lock:
            if self.schema is not None:
                raise RuntimeError(
                    "Schema already set; cannot call set_schema more than once."
                )
            try:
                writer = pa.ipc.new_stream(self.sink, schema)
            except Exception as exc:
                raise RuntimeError("Failed to initialize Arrow stream writer.") from exc
            self.schema = schema
            self.writer = writer

    def _write(self) -> None:
        """Write buffered rows to the file.

        Must be called with ``self._lock`` held.
        """
        if len(self.new_rows) > 0:
            if self.schema is None:
                # only need to look at the first row to learn the schema
                self.set_schema(pa.Table.from_pylist([self.new_rows[0]]).schema)

            if self.writer is None:
                raise RuntimeError(
                    "Writer is None despite schema being set; schema initialization may have failed."
                )
            self.writer.write_batch(
                pa.RecordBatch.from_pylist(  # type: ignore[attr-defined]
                    self.new_rows, schema=self.schema
                )
            )
            self.new_rows = []

    def add_row(self, row_dict: dict) -> None:
        """Add a row to the arrow file.

        We will automatically learn the schema from the first row. But all rows must use that schema.

        Parameters
        ----------
        row_dict : dict
            Dictionary representing a single row with field names matching the schema.
        """
        with self._lock:
            self.new_rows.append(row_dict)
            if len(self.new_rows) >= CHUNK_SIZE:
                self._write()


class FeatherWriter(ArrowWriter):
    """A smaller more interoperable version of arrow files.

    Uses a temporary .arrow file (which could be huge) but converts to a much smaller (but still fast)
    feather file.
    """

    def __init__(self, file_name: str):
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

    def close(self) -> None:
        """Close the writer and convert the temporary Arrow file to Feather format.

        Converts the temporary .arrow file to a compressed .feather file and
        removes the temporary file. Empty Arrow files are discarded.
        """
        super().close()
        src_name = self.base_file_name + ".arrow"
        dest_name = self.base_file_name + ".feather"
        if os.path.getsize(src_name) == 0:
            logging.warning("Discarding empty file: %s", src_name)
            os.remove(src_name)
        else:
            logging.info("Compressing log data into %s", dest_name)

            # note: must use open_stream, not open_file/read_table because the streaming layout is different
            # data = feather.read_table(src_name)
            with pa.memory_map(src_name) as source:
                reader = pa.ipc.open_stream(source)
                try:
                    array = reader.read_all()
                finally:
                    reader.close()

            # See https://stackoverflow.com/a/72406099 for more info and performance testing measurements
            feather.write_feather(array, dest_name, compression="zstd")  # type: ignore[arg-type]
            os.remove(src_name)
