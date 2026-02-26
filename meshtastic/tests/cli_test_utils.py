"""Helpers for invoking CLI commands in tests with bounded execution time."""

import subprocess

import pytest


def run_cli_with_timeout(command: str, timeout: int = 120) -> tuple[int, str]:
    """Run a shell CLI command and return (exit_code, combined_output).

    Parameters
    ----------
    command : str
        Full shell command to execute.
    timeout : int
        Maximum time to allow command execution before failing the test.
        (Default value = 120)

    Returns
    -------
    tuple[int, str]
        Exit code and combined stdout/stderr output.
    """
    try:
        result = subprocess.run(  # noqa: S602
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"CLI command timed out after {timeout}s: {command!r} ({exc})")
        raise  # pragma: no cover - pytest.fail always raises
    return result.returncode, result.stdout
