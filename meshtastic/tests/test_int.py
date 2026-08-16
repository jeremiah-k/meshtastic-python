"""Meshtastic CLI integration tests."""

import re

import pytest

from meshtastic._branding import PRIMARY_CLI_NAME, UPSTREAM_PRODUCT_NAME

from .cli_test_utils import run_cli_argv_with_timeout


def _run_and_collect(cmd: list[str]) -> tuple[int, str]:
    """Run CLI argv command and return (returncode, combined output)."""
    result = run_cli_argv_with_timeout(cmd)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


@pytest.mark.int
def test_int_primary_cli_no_args(primary_cli_bin: str) -> None:
    """The preferred CLI entry point should own its argparse program name."""
    returncode, output = _run_and_collect([primary_cli_bin])
    assert output.startswith(f"usage: {PRIMARY_CLI_NAME}")
    assert returncode == 1


@pytest.mark.int
def test_int_meshtastic_no_args(meshtastic_bin: str) -> None:
    """The historical Meshtastic CLI entry point should remain functional."""
    returncode, output = _run_and_collect([meshtastic_bin])
    assert output.startswith(f"usage: {UPSTREAM_PRODUCT_NAME}")
    assert returncode == 1


@pytest.mark.int
def test_int_mesh_tunnel_no_args(mesh_tunnel_bin: str) -> None:
    """Test mesh-tunnel without any args."""
    returncode, output = _run_and_collect([mesh_tunnel_bin])
    assert output.startswith("usage: mesh-tunnel")
    assert returncode == 1


@pytest.mark.int
@pytest.mark.parametrize("fixture_name", ("primary_cli_bin", "meshtastic_bin"))
def test_int_version_is_branded(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    """Both CLI names should report the configured product identity and version."""
    cli_bin = request.getfixturevalue(fixture_name)
    returncode, output = _run_and_collect([cli_bin, "--version"])
    assert re.fullmatch(
        rf"{re.escape(PRIMARY_CLI_NAME)} [0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9A-Za-z]+)*\n?",
        output,
    )
    assert returncode == 0


@pytest.mark.int
def test_int_help(primary_cli_bin: str) -> None:
    """The preferred CLI should render help under its preferred executable name."""
    returncode, output = _run_and_collect([primary_cli_bin, "--help"])
    assert output.startswith(f"usage: {PRIMARY_CLI_NAME} ")
    assert returncode == 0


@pytest.mark.int
def test_int_support(primary_cli_bin: str) -> None:
    """Support output should work through the preferred CLI."""
    returncode, output = _run_and_collect([primary_cli_bin, "--support"])
    assert "System" in output
    assert "Python" in output
    assert returncode == 0
