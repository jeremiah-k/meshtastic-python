"""Helpers for invoking CLI commands in tests with bounded execution time."""

import re
import shlex
import subprocess

import pytest

EMPTY_SHELL_COMMAND_ERROR = "Empty command passed to CLI shell helper"
EMPTY_ARGV_COMMAND_ERROR = "cmd must not be empty"
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


def _fail_masked_timeout(command_name: str, timeout_s: int | float) -> None:
    """Fail a test with a redacted timeout message for a CLI command."""
    masked = f"{command_name!r} +REDACTED_ARGS"
    pytest.fail(f"CLI command timed out after {timeout_s}s: {masked}")


def _shell_executable_for_timeout(command: str) -> str:
    """Return a safe executable token for timeout messages without leaking env assignments."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if not _ENV_ASSIGNMENT_RE.fullmatch(token):
            return token
    return "<shell-command>"


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

    Raises
    ------
    ValueError
        If command is empty or whitespace-only.
    """
    if not command.strip():
        raise ValueError(EMPTY_SHELL_COMMAND_ERROR)
    try:
        # Intentional shell=True for legacy string-based test commands.
        # For argv + shell=False patterns, use run_cli_argv_with_timeout.
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
        executable = _shell_executable_for_timeout(command)
        _fail_masked_timeout(executable, timeout)
        raise  # pragma: no cover - pytest.fail always raises
    return result.returncode, result.stdout


def run_cli_argv_with_timeout(
    cmd: list[str], timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """Run a CLI command using argv list and return CompletedProcess.

    Parameters
    ----------
    cmd : list[str]
        Command and arguments as a list (e.g., ["meshtastic", "--info"]).
    timeout : int
        Maximum time to allow command execution before failing the test.
        (Default value = 30)

    Returns
    -------
    subprocess.CompletedProcess[str]
        CompletedProcess with returncode, stdout, and stderr attributes.

    Raises
    ------
    ValueError
        If cmd is empty.
    """
    if not cmd:
        raise ValueError(EMPTY_ARGV_COMMAND_ERROR)
    try:
        return subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        cmd_name = cmd[0]
        timeout_value = e.timeout if e.timeout is not None else timeout
        _fail_masked_timeout(cmd_name, timeout_value)
        raise  # pragma: no cover - pytest.fail always raises
