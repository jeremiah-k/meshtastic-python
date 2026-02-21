"""code logging power consumption of meshtastic devices."""

import atexit
import io
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from pathlib import Path

import parse  # type: ignore[import-untyped]
import platformdirs
import pyarrow as pa
from pubsub import pub  # type: ignore[import-untyped]

from meshtastic.mesh_interface import MeshInterface
from meshtastic.powermon import PowerMeter

from .arrow import FeatherWriter

logger = logging.getLogger(__name__)


def root_dir() -> str:
    """
    Determine and ensure the root slog directory exists under the user data directory.

    Returns
    -------
    str
        Filesystem path to the "slogs" directory.

    """

    app_name = "meshtastic"
    app_author = "meshtastic"
    app_dir = platformdirs.user_data_dir(app_name, app_author)
    dir_name = Path(app_dir, "slogs")
    dir_name.mkdir(exist_ok=True, parents=True)
    return str(dir_name)


@dataclass(init=False)
class LogDef:
    """Log definition."""

    code: str  # i.e. PM or B or whatever... see meshtastic slog documentation
    fields: list[tuple[str, pa.DataType]]  # A list of field names and their arrow types
    format: parse.Parser  # A format string that can be used to parse the arguments

    def __init__(self, code: str, fields: list[tuple[str, pa.DataType]]) -> None:
        """Initialize the LogDef object.

        Parameters
        ----------
            code (str): The code.
            fields (list[tuple[str, pa.DataType]]): Field name/type pairs used to build the parser format.

        """
        self.code = code
        self.fields = fields

        fmt = ""
        for idx, f in enumerate(fields):
            if idx != 0:
                fmt += ","

            # make the format string
            suffix = (
                "" if f[1] == pa.string() else ":d"
            )  # treat as a string or an int (the only types we have so far)
            fmt += "{" + f[0] + suffix + "}"
        self.format = parse.compile(
            fmt
        )  # We include a catchall matcher at the end - to ignore stuff we don't understand


"""A dictionary mapping from logdef code to logdef"""
log_defs = {
    d.code: d
    for d in [
        LogDef("B", [("board_id", pa.uint32()), ("sw_version", pa.string())]),
        LogDef("PM", [("pm_mask", pa.uint64()), ("pm_reason", pa.string())]),
        LogDef("PS", [("ps_state", pa.uint32())]),
    ]
}
log_regex = re.compile(".*S:([0-9A-Za-z]+):(.*)")


class PowerLogger:
    """Logs current watts reading periodically using PowerMeter and ArrowWriter."""

    def __init__(self, pMeter: PowerMeter, file_path: str, interval=0.002) -> None:
        """Initialize the PowerLogger object."""
        self.pMeter = pMeter
        self.writer = FeatherWriter(file_path)
        self.interval = interval
        self.is_logging = True
        self.thread = threading.Thread(
            target=self._logging_thread, name="PowerLogger", daemon=True
        )
        self.thread.start()

    def store_current_reading(self, now: datetime | None = None) -> None:
        """Store current power measurement."""
        if now is None:
            now = datetime.now()
        d = {
            "time": now,
            "average_mW": self.pMeter.get_average_current_mA(),
            "max_mW": self.pMeter.get_max_current_mA(),
            "min_mW": self.pMeter.get_min_current_mA(),
        }
        self.pMeter.reset_measurements()
        self.writer.add_row(d)

    def _logging_thread(self) -> None:
        """Background thread for logging the current watts reading."""
        while self.is_logging:
            self.store_current_reading()
            time.sleep(self.interval)

    def close(self) -> None:
        """Close the PowerLogger and stop logging."""
        if self.is_logging:
            self.pMeter.close()
            self.is_logging = False
            self.thread.join()
            self.writer.close()


# FIXME move these defs somewhere else
TOPIC_MESHTASTIC_LOG_LINE = "meshtastic.log.line"


class StructuredLogger:
    """Sniffs device logs for structured log messages, extracts those into apache arrow format.
    Also writes the raw log messages to raw.txt."""

    def __init__(
        self,
        client: MeshInterface,
        dir_path: str,
        power_logger: PowerLogger | None = None,
        include_raw: bool = True,
    ) -> None:
        """
        Create a StructuredLogger that monitors device logs and writes structured entries to an Arrow writer.

        Parameters
        ----------
        client : MeshInterface
            Source of device log lines to monitor.
        dir_path : str
            Filesystem directory where the slog Arrow dataset and optional raw.txt are created.
        power_logger : PowerLogger | None, optional
            If provided, used to record a power sample with each structured log entry.
        include_raw : bool, optional
            If True, include a "raw" string field in the schema and write raw log lines to raw.txt.

        """
        self.client = client
        self.power_logger = power_logger

        # Setup the arrow writer (and its schema)
        self.writer = FeatherWriter(os.path.join(dir_path, "slog"))
        all_fields = reduce(
            (lambda x, y: x + y), map(lambda x: x.fields, log_defs.values())
        )

        self.include_raw = include_raw
        if self.include_raw:
            all_fields.append(("raw", pa.string()))

        # Use timestamp as the first column
        all_fields.insert(0, ("time", pa.timestamp("us")))

        # pass in our name->type tuples a pa.fields
        self.writer.set_schema(
            pa.schema(map(lambda x: pa.field(x[0], x[1]), all_fields))
        )

        self._raw_file_lock = threading.Lock()
        self.raw_file: io.TextIOWrapper | None = (
            open(  # pylint: disable=consider-using-with
                os.path.join(dir_path, "raw.txt"), "w", encoding="utf8"
            )
            if self.include_raw
            else None
        )

        # We need a closure here because the subscription API is very strict about exact arg matching
        def listen_glue(line, interface):  # pylint: disable=unused-argument
            self._onLogMessage(line)

        self._listen_glue = (
            listen_glue  # we must save this so it doesn't get garbage collected
        )
        try:
            pub.subscribe(self._listen_glue, TOPIC_MESHTASTIC_LOG_LINE)
        except Exception:
            # If subscription fails, close file handles before re-raising
            if self.raw_file:
                self.raw_file.close()
                self.raw_file = None
            if self.writer:
                self.writer.close()
            raise

    def close(self) -> None:
        """
        Shut down the StructuredLogger and release its resources.

        Unsubscribes the log listener, closes the Arrow writer, and safely closes and clears the
        raw log file reference while holding the internal lock so concurrent writers cannot race
        with shutdown.
        """
        try:
            pub.unsubscribe(self._listen_glue, TOPIC_MESHTASTIC_LOG_LINE)
        finally:
            try:
                self.writer.close()
            finally:
                with self._raw_file_lock:
                    f = self.raw_file
                    self.raw_file = None  # mark that we are shutting down
                if f:
                    f.close()  # Close the raw.txt file

    def _onLogMessage(self, line: str) -> None:
        """
        Process a single raw log line, extract any structured slog fields, and persist the resulting record.

        Parses the input line for a structured slog. If parsing yields fields, adds a "time"
        timestamp and writes the record to the configured Arrow writer. If raw logging is enabled,
        includes the original raw line in the record and appends it to the raw log file. If a power
        logger is present, records a power measurement using the exact same timestamp as the
        written slog record. Unknown or unparsable structured slog lines are logged as warnings.

        Parameters
        ----------
        line : str
            The raw log line to process.

        """

        di = {}  # the dictionary of the fields we found to log

        m = log_regex.match(line)
        if m:
            src = m.group(1)
            args = m.group(2)
            logger.debug(f"SLog {src}, args: {args}")

            d = log_defs.get(src)
            if d:
                last_field = d.fields[-1]
                last_is_str = last_field[1] == pa.string()
                if last_is_str:
                    args += " "
                    # append a space so that if the last arg is an empty str
                    # it will still be accepted as a match for a str

                r = d.format.parse(args)  # get the values with the correct types
                if r:
                    di = r.named  # type: ignore[union-attr] # pyright: ignore[reportAttributeAccessIssue]
                    if last_is_str:
                        di[last_field[0]] = di[
                            last_field[0]
                        ].strip()  # remove the trailing space we added
                        if di[last_field[0]] == "":
                            # If the last field is an empty string, remove it
                            del di[last_field[0]]
                else:
                    logger.warning(f"Failed to parse slog {line} with {d.format}")
            else:
                logger.warning(f"Unknown Structured Log: {line}")

        # Store our structured log record
        if di or self.include_raw:
            now = datetime.now()
            di["time"] = now
            if self.include_raw:
                di["raw"] = line
            self.writer.add_row(di)

            # If we have a sibling power logger, make sure we have a power measurement with the EXACT same timestamp
            if self.power_logger:
                self.power_logger.store_current_reading(now)

        # Only acquire lock and write if raw logging is enabled
        if self.include_raw:
            with self._raw_file_lock:
                if self.raw_file:
                    self.raw_file.write(line + "\n")  # Write the raw log


class LogSet:
    """A complete set of meshtastic log/metadata for a particular run."""

    def __init__(
        self,
        client: MeshInterface,
        dir_name: str | None = None,
        power_meter: PowerMeter | None = None,
    ) -> None:
        """
        Create a LogSet: prepare a directory for slog files, start structured slogging, and optionally start power logging.

        If dir_name is not provided, a timestamped directory is created under the slog root and a
        "latest" symlink is updated to point to it. A StructuredLogger is created and bound to the
        provided client; if power_meter is supplied, a PowerLogger is created that writes to a
        "power" subdirectory. An atexit handler pointing to this instance's close() is registered
        for later teardown.

        Parameters
        ----------
        client : MeshInterface
            MeshInterface client whose log lines will be monitored and recorded.
        dir_name : str | None, optional
            Path for storing logs; when omitted a new timestamped directory is
            created under the slog root and "latest" is updated to point to it.
        power_meter : PowerMeter | None, optional
            When provided, a PowerLogger is started to record power samples alongside slog entries.

        """

        if not dir_name:
            app_dir = root_dir()
            app_time_dir = Path(app_dir, datetime.now().strftime("%Y%m%d-%H%M%S"))
            app_time_dir.mkdir(exist_ok=True)
            dir_name = str(app_time_dir)

            # Also make a 'latest' directory that always points to the most recent logs
            latest_dir = Path(app_dir, "latest")
            latest_dir.unlink(missing_ok=True)

            # symlink might fail on some platforms, if it does fail silently
            try:
                latest_dir.symlink_to(dir_name, target_is_directory=True)
            except OSError:
                pass

        self.dir_name = dir_name

        logger.info(f"Writing slogs to {dir_name}")

        self.power_logger: PowerLogger | None = (
            None
            if not power_meter
            else PowerLogger(power_meter, os.path.join(self.dir_name, "power"))
        )

        try:
            self.slog_logger: StructuredLogger | None = StructuredLogger(
                client, self.dir_name, power_logger=self.power_logger
            )
        except Exception:
            if self.power_logger:
                self.power_logger.close()
                self.power_logger = None
            raise

        # Store a lambda so we can find it again to unregister
        self.atexit_handler = lambda: self.close()  # pylint: disable=unnecessary-lambda
        atexit.register(self.atexit_handler)

    def close(self) -> None:
        """Close the log set."""

        if self.slog_logger:
            logger.info(f"Closing slogs in {self.dir_name}")
            atexit.unregister(
                self.atexit_handler
            )  # docs say it will silently ignore if not found
            self.slog_logger.close()
            if self.power_logger:
                self.power_logger.close()
            self.slog_logger = None
