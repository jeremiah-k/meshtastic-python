"""Contracts for fork-adjustable product and CLI branding."""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from meshtastic import _branding as branding

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _TTYStringIO(StringIO):
    """String buffer that behaves like an interactive terminal."""

    def isatty(self) -> bool:
        """Report an interactive stream for compatibility-notice tests."""
        return True


@pytest.mark.unit
def test_product_name_is_single_runtime_branding_switch() -> None:
    """Distribution, primary CLI, and display branding should share one source."""
    assert branding.DISTRIBUTION_NAME == branding.PRODUCT_NAME
    assert branding.PRIMARY_CLI_NAME == branding.PRODUCT_NAME
    expected_display_name = (
        branding.UPSTREAM_DISPLAY_NAME
        if branding.PRODUCT_NAME == branding.UPSTREAM_PRODUCT_NAME
        else f"{branding.UPSTREAM_DISPLAY_NAME} ({branding.PRODUCT_NAME} fork)"
    )
    assert branding.PROJECT_DISPLAY_NAME == expected_display_name
    expected_repository = (
        branding.UPSTREAM_REPOSITORY_URL
        if branding.PRODUCT_NAME == branding.UPSTREAM_PRODUCT_NAME
        else branding.FORK_REPOSITORY_URL
    )
    assert branding.PROJECT_REPOSITORY_URL == expected_repository
    assert branding.PROJECT_ISSUE_URL == f"{expected_repository}/issues"

    if branding.PRODUCT_NAME == branding.UPSTREAM_PRODUCT_NAME:
        assert branding.COMPATIBILITY_CLI_NAMES == ()
    else:
        assert branding.COMPATIBILITY_CLI_NAMES == (branding.UPSTREAM_PRODUCT_NAME,)


@pytest.mark.unit
def test_format_cli_version_includes_product_identity() -> None:
    """Version output should identify the distribution, not print a bare version."""
    assert branding._format_cli_version("2.7.11.post5") == (
        f"{branding.PRIMARY_CLI_NAME} 2.7.11.post5"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv0",
    (
        branding.UPSTREAM_PRODUCT_NAME,
        f"/usr/local/bin/{branding.UPSTREAM_PRODUCT_NAME}",
        rf"C:\Users\Example\bin\{branding.UPSTREAM_PRODUCT_NAME.upper()}.EXE",
    ),
)
def test_compatibility_cli_notice_recognizes_legacy_invocations(argv0: str) -> None:
    """Compatibility executable paths should receive the preferred-command hint."""
    notice = branding._compatibility_cli_notice(argv0)
    if not branding.COMPATIBILITY_CLI_NAMES:
        assert notice is None
        return
    assert notice is not None
    assert branding.PRIMARY_CLI_NAME in notice
    assert "No shell alias is required" in notice


@pytest.mark.unit
def test_compatibility_cli_notice_ignores_primary_and_module_invocations() -> None:
    """Preferred and module-style launchers should not produce migration noise."""
    assert branding._compatibility_cli_notice(branding.PRIMARY_CLI_NAME) is None
    assert branding._compatibility_cli_notice("__main__.py") is None


@pytest.mark.unit
def test_compatibility_notice_is_interactive_only() -> None:
    """Legacy scripts must not gain new stderr output while terminals get guidance."""
    compatibility_name = next(iter(branding.COMPATIBILITY_CLI_NAMES), None)
    if compatibility_name is None:
        pytest.skip("configured product has no compatibility CLI")

    interactive = _TTYStringIO()
    assert branding._emit_compatibility_cli_notice(
        compatibility_name, stream=interactive
    )
    assert branding.PRIMARY_CLI_NAME in interactive.getvalue()

    non_interactive = StringIO()
    assert not branding._emit_compatibility_cli_notice(
        compatibility_name, stream=non_interactive
    )
    assert non_interactive.getvalue() == ""


@pytest.mark.unit
def test_packaging_metadata_tracks_branding_contract() -> None:
    """Static install-time metadata must stay synchronized with runtime branding."""
    pyproject = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    poetry = pyproject["tool"]["poetry"]
    scripts = poetry["scripts"]

    assert poetry["name"] == branding.DISTRIBUTION_NAME
    expected_cli_names = {
        branding.PRIMARY_CLI_NAME,
        *branding.COMPATIBILITY_CLI_NAMES,
    }
    actual_cli_names = {
        name for name, target in scripts.items() if target == "meshtastic.__main__:main"
    }
    assert actual_cli_names == expected_cli_names
