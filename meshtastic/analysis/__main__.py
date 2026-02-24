"""Post-run analysis tools for meshtastic."""

import argparse
import ipaddress
import logging
import os
from collections.abc import Iterable
from typing import Any, NoReturn, cast

import dash_bootstrap_components as dbc  # type: ignore[import-untyped]
import numpy as np
import pandas as pd
import plotly.express as px  # type: ignore[import-untyped]
import plotly.graph_objects as go  # type: ignore[import-untyped]
import pyarrow as pa
from dash import Dash, dcc, html  # type: ignore[import-untyped]
from pyarrow import feather

from .. import mesh_pb2, powermon_pb2, util
from ..slog import rootDir

# Configure panda options
pd.options.mode.copy_on_write = True


def _cli_exit(message: str, return_value: int = 1) -> NoReturn:
    """Exit this analysis CLI entrypoint with a user-facing message.

    Parameters
    ----------
    message : str
        Message to print before exiting.
    return_value : int
        Process exit code (0 for success, non-zero for error).
    """
    util.our_exit(message, return_value)


def to_pmon_names(arr: Iterable[Any]) -> list[str | None]:
    """Map a sequence of power-monitor state integers to their enum name strings.

    Parameters
    ----------
    arr : Iterable[Any]
        Sequence of values convertible to int representing power-monitor states.

    Returns
    -------
    list[str | None]
        List of corresponding enum names; elements are `None`
        when a value cannot be mapped or represents the "None" state.
    """

    def to_pmon_name(n: Any) -> str | None:
        """Map a power-monitor state numeric value to its corresponding enum name.

        Parameters
        ----------
        n : Any
            Numeric value (or value convertible to int) representing a PowerMon.State.

        Returns
        -------
        str | None
            The enum name string for the given state, or `None` if the value does not map to a known state.
        """
        try:
            s = powermon_pb2.PowerMon.State.Name(cast(Any, int(n)))
        except (ValueError, TypeError):
            return None
        else:
            return s if s != "None" else None

    return [to_pmon_name(x) for x in arr]


def read_pandas(filepath: str) -> pd.DataFrame:
    """Load a Feather file and map Arrow column types to pandas nullable dtypes to preserve nullability.

    Converts Arrow column types to appropriate pandas extension dtypes (e.g., nullable
    integer, boolean, and string dtypes) so that integer/boolean columns retain
    nullability instead of being cast to float.

    Parameters
    ----------
    filepath : str
        Path to the Feather file to read.

    Returns
    -------
    pd.DataFrame
        DataFrame whose columns use pandas nullable dtypes where applicable.
    """
    # per https://arrow.apache.org/docs/python/pandas.html#reducing-memory-use-in-table-to-pandas
    # use this to get nullable int fields treated as ints rather than floats in pandas
    dtype_mapping = {
        pa.int8(): pd.Int8Dtype(),
        pa.int16(): pd.Int16Dtype(),
        pa.int32(): pd.Int32Dtype(),
        pa.int64(): pd.Int64Dtype(),
        pa.uint8(): pd.UInt8Dtype(),
        pa.uint16(): pd.UInt16Dtype(),
        pa.uint32(): pd.UInt32Dtype(),
        pa.uint64(): pd.UInt64Dtype(),
        pa.bool_(): pd.BooleanDtype(),
        pa.float32(): pd.Float32Dtype(),
        pa.float64(): pd.Float64Dtype(),
        pa.string(): pd.ArrowDtype(pa.string()),
    }

    def _types_mapper(
        data_type: pa.DataType,
    ) -> pd.api.extensions.ExtensionDtype | None:
        """Map a PyArrow DataType to a pandas nullable ExtensionDtype.

        Parameters
        ----------
        data_type : pa.DataType
            The Arrow data type to map.

        Returns
        -------
        pd.api.extensions.ExtensionDtype | None
            The corresponding pandas nullable extension
            dtype if a predefined mapping exists; otherwise a pandas ArrowDtype wrapping
            the provided Arrow type.
        """
        mapped = dtype_mapping.get(data_type)
        if mapped is not None:
            return mapped
        return pd.ArrowDtype(data_type)

    return cast(
        pd.DataFrame,
        feather.read_table(filepath).to_pandas(types_mapper=cast(Any, _types_mapper)),
    )


def get_pmon_raises(dslog: pd.DataFrame) -> pd.DataFrame:
    """Extract rows where one or more power monitors transitioned to the raised state.

    Parameters
    ----------
    dslog : pd.DataFrame
        Slog DataFrame that must contain `pm_mask` and `time` columns.

    Returns
    -------
    pd.DataFrame
        Subset of rows where power-monitor raises occurred.
        Columns are `time` and `pm_raises` (a list of raised power-monitor
        name strings).
    """
    pmon_events = dslog[dslog["pm_mask"].notnull()].copy()

    pm_masks = pd.Series(pmon_events["pm_mask"]).to_numpy()

    # possible to do this with pandas rolling windows if I was smarter?
    pm_changes = [
        (pm_masks[i - 1] ^ x if i != 0 else x) for i, x in enumerate(pm_masks)
    ]
    pm_raises = [(pm_masks[i] & x) for i, x in enumerate(pm_changes)]
    pm_falls = [(~pm_masks[i] & x if i != 0 else 0) for i, x in enumerate(pm_changes)]

    pmon_events["pm_raises"] = to_pmon_names(pm_raises)
    pmon_events["pm_falls"] = to_pmon_names(pm_falls)

    pmon_raises = pmon_events[pmon_events["pm_raises"].notnull()][["time", "pm_raises"]]
    return pmon_raises


def get_board_info(dslog: pd.DataFrame) -> tuple[str, str]:
    """Retrieve board model name and software version from a slog DataFrame.

    Parameters
    ----------
    dslog : pd.DataFrame
        Slog DataFrame containing at least the 'sw_version' and 'board_id' columns.

    Returns
    -------
    tuple[str, str]
        (board_model_name, sw_version) where `board_model_name` is the
        HardwareModel enum name string derived from `board_id`, and `sw_version`
        is the software version string.
    """
    if "sw_version" not in dslog.columns:
        raise ValueError("No sw_version column found in slog")  # noqa: TRY003
    board_info = dslog[dslog["sw_version"].notnull()]
    if board_info.empty:
        raise ValueError("No board info rows found in slog")  # noqa: TRY003
    if "board_id" not in board_info.columns or board_info["board_id"].isna().all():
        raise ValueError("No board_id rows found in slog")  # noqa: TRY003
    board_info = board_info[board_info["board_id"].notnull()]
    first_row = board_info.iloc[0]
    sw_version = str(first_row["sw_version"])
    board_id = mesh_pb2.HardwareModel.Name(cast(Any, int(first_row["board_id"])))
    return (board_id, sw_version)


def choose_power_column(frame: pd.DataFrame, legacy_name: str, new_name: str) -> str:
    """Choose a power-series column while preserving compatibility.

    Historical logs used ``*_mW`` field names. New logs expose corrected
    ``*_mA`` names. Prefer the legacy column when present and non-empty to keep
    existing datasets stable, otherwise fall back to the corrected name.

    Parameters
    ----------
    frame : pd.DataFrame
        DataFrame whose columns are searched.
    legacy_name : str
        Name of the historical column (e.g. ``"average_mW"``).
    new_name : str
        Name of the replacement column (e.g. ``"average_mA"``).

    Returns
    -------
    str
        The chosen column name.

    Raises
    ------
    ValueError
        If neither ``legacy_name`` nor ``new_name`` is present in ``frame``.
    """
    if legacy_name in frame.columns and not frame[legacy_name].isna().all():
        return legacy_name
    if new_name in frame.columns:
        return new_name
    if legacy_name in frame.columns:
        # Keep compatibility with legacy-only logs even if values are all null.
        return legacy_name
    error_msg = (
        "Missing required power column. Expected one of "
        f"{legacy_name!r} or {new_name!r}; available columns: {list(frame.columns)!r}"
    )
    raise ValueError(error_msg)


def create_argparser() -> argparse.ArgumentParser:
    """Create the command-line argument parser for analysis tools.

    Returns
    -------
    argparse.ArgumentParser
        Configured argument parser instance.
    """
    parser = argparse.ArgumentParser(description="Meshtastic power analysis tools")
    group = parser
    group.add_argument(
        "--slog",
        help="Specify the structured-logs directory (defaults to latest log directory)",
    )
    group.add_argument(
        "--no-server",
        action="store_true",
        help="Exit immediately, without running the visualization web server",
    )
    group.add_argument(
        "--bind-host",
        "--host",
        default="127.0.0.1",
        dest="host",
        help="Bind address for the Dash server (default: 127.0.0.1). Use 0.0.0.0 for remote access.",
    )
    group.add_argument(
        "--debug",
        action="store_true",
        help="Enable Dash debug mode (safe only on localhost).",
    )
    group.add_argument(
        "--port",
        "-p",
        type=int,
        default=8051,
        dest="port",
        help="Port for the Dash server (default: 8051).",
    )
    group.add_argument(
        "--dest",
        help="Specify target node (required by CLI contract, unused by analysis).",
    )

    return parser


def create_dash(slog_path: str) -> Dash:
    """Create a Dash application that visualizes Meshtastic power data from a slog directory.

    Parameters
    ----------
    slog_path : str
        Path to the slog directory containing `power.feather` and `slog.feather`.

    Returns
    -------
    Dash
        Configured Dash application with line and scatter traces for average, max, and min power and markers for power-monitor raise events.
    """
    app = Dash(external_stylesheets=[dbc.themes.BOOTSTRAP])

    dpwr = read_pandas(os.path.join(slog_path, "power.feather"))
    dslog = read_pandas(os.path.join(slog_path, "slog.feather"))

    pmon_raises = get_pmon_raises(dslog)

    def set_legend(f: go.Figure, name: str) -> go.Figure:
        """Set the legend name and enable legend display for the first trace in a figure.

        Parameters
        ----------
        f : plotly.graph_objects.Figure
            The Plotly figure to modify.
        name : str
            The legend name to set for the first trace.

        Returns
        -------
        plotly.graph_objects.Figure
            The modified figure with updated legend settings.
        """
        f["data"][0]["showlegend"] = True
        f["data"][0]["name"] = name
        return f

    avg_col = choose_power_column(dpwr, legacy_name="average_mW", new_name="average_mA")
    max_col = choose_power_column(dpwr, legacy_name="max_mW", new_name="max_mA")
    min_col = choose_power_column(dpwr, legacy_name="min_mW", new_name="min_mA")

    avg_pwr_lines = px.line(dpwr, x="time", y=avg_col).update_traces(line_color="red")
    set_legend(avg_pwr_lines, "avg power")
    max_pwr_points = px.scatter(dpwr, x="time", y=max_col).update_traces(
        marker_color="blue"
    )
    set_legend(max_pwr_points, "max power")
    min_pwr_points = px.scatter(dpwr, x="time", y=min_col).update_traces(
        marker_color="green"
    )
    set_legend(min_pwr_points, "min power")

    fake_y = np.full(len(pmon_raises), 10.0)
    pmon_points = px.scatter(pmon_raises, x="time", y=fake_y, text="pm_raises")

    figure_data = (
        list(max_pwr_points.data)
        + list(avg_pwr_lines.data)
        + list(min_pwr_points.data)
        + list(pmon_points.data)
    )
    fig = go.Figure(data=figure_data)

    fig.update_layout(
        legend={"yanchor": "top", "y": 0.99, "xanchor": "left", "x": 0.01}
    )

    # App layout
    app.layout = [
        html.Div(children="Meshtastic power analysis tool testing..."),
        dcc.Graph(figure=fig),
    ]

    return app


def _is_loopback_host(host_value: str) -> bool:
    """Return True when the provided host value resolves to a loopback address."""
    host_stripped = host_value.strip()
    if host_stripped.startswith("[") and host_stripped.endswith("]"):
        host_stripped = host_stripped[1:-1]
    if host_stripped.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host_stripped).is_loopback
    except ValueError:
        return False


def main() -> None:
    """Entry point of the script.

    Parses command-line arguments, reads the slog data, and optionally starts
    a Dash web server for visualization. Expected startup/data-loading
    exceptions are converted to user-facing CLI exits via `_cli_exit()`.
    """
    parser = create_argparser()
    args = parser.parse_args()
    if not args.slog:
        args.slog = os.path.join(rootDir(), "latest")

    try:
        app = create_dash(slog_path=args.slog)
    except (ValueError, FileNotFoundError, OSError, pa.ArrowException) as exc:
        _cli_exit(f"Error loading slog data: {exc}")

    port = args.port
    debug = args.debug
    host = args.host

    if not _is_loopback_host(host) and debug:
        logging.warning(
            "Ignoring --debug because host %s is not localhost; running with debug disabled.",
            host,
        )
        debug = False
    logging.info(
        "Running Dash visualization of %s on %s:%d (debug=%s)",
        args.slog,
        host,
        port,
        debug,
    )

    if not args.no_server:
        app.run(debug=debug, host=host, port=port)
    else:
        logging.info("Exiting without running visualization server")


if __name__ == "__main__":
    main()
