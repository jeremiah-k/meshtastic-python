"""Helpers for invoking CLI commands in tests with bounded execution time."""

import shlex
import subprocess

import pytest


def run_cli_with_timeout(command: str, timeout: int = 120) -> tuple[int, str]:
    """Run a shell CLI command and return (exit_code, combined_output).

    Parameters
    ----------
    command : str
        Full shell command to execute. This helper intentionally accepts
        a command string (rather than argv tokens) for compatibility with
        existing test call sites; use `shlex.quote` for path quoting
        when constructing commands that may include spaces.
    timeout : int
        Maximum time to allow command execution before failing the test.
        (Default value = 120)

    Returns
    -------
    tuple[int, str]
        Exit code and combined stdout/stderr output.
    """
    try:
        # Intentional shell=True for legacy string-based test commands.
        # For argv + shell=False patterns, see _run_cli_with_timeout in test_int.py.
        result = subprocess.run(  # noqa: S602
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        try:
            executable = shlex.split(command)[0]
        except (ValueError, IndexError):
            executable = command.split(" ", 1)[0] if command else "<unknown>"
        masked = f"{executable!r} +REDACTED_ARGS"
        pytest.fail(f"CLI command timed out after {timeout}s: {masked}")
        raise  # pragma: no cover - pytest.fail always raises
    return result.returncode, result.stdout
