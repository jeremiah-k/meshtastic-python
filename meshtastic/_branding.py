"""Centralized product and CLI branding for fork-adjustable user-facing surfaces.

The Python import namespace intentionally remains ``meshtastic`` for public API
compatibility.  ``PRODUCT_NAME`` controls the fork-specific distribution/CLI
branding used by runtime output.  If this work is carried upstream, changing
``PRODUCT_NAME`` to ``"meshtastic"`` collapses the compatibility CLI list and
restores upstream-facing runtime branding in one place.

Install-time metadata such as the console-script keys in ``pyproject.toml``
cannot reference Python constants, so tests enforce that those static entries
stay synchronized with this module.
"""

from __future__ import annotations

# Single runtime branding switch. Keep the import namespace as ``meshtastic``.
PRODUCT_NAME: str = "mtjk"

# Upstream identity used to preserve a compatibility command while this fork has
# a distinct product name.
UPSTREAM_PRODUCT_NAME: str = "meshtastic"
UPSTREAM_DISPLAY_NAME: str = "Meshtastic"

DISTRIBUTION_NAME: str = PRODUCT_NAME
PRIMARY_CLI_NAME: str = PRODUCT_NAME
PROJECT_DISPLAY_NAME: str = (
    UPSTREAM_DISPLAY_NAME
    if PRODUCT_NAME == UPSTREAM_PRODUCT_NAME
    else f"{UPSTREAM_DISPLAY_NAME} ({PRODUCT_NAME} fork)"
)
FORK_REPOSITORY_URL: str = "https://github.com/jeremiah-k/mtjk"
UPSTREAM_REPOSITORY_URL: str = "https://github.com/meshtastic/python"
PROJECT_REPOSITORY_URL: str = (
    UPSTREAM_REPOSITORY_URL
    if PRODUCT_NAME == UPSTREAM_PRODUCT_NAME
    else FORK_REPOSITORY_URL
)
PROJECT_ISSUE_URL: str = f"{PROJECT_REPOSITORY_URL}/issues"
COMPATIBILITY_CLI_NAMES: tuple[str, ...] = (
    () if PRIMARY_CLI_NAME == UPSTREAM_PRODUCT_NAME else (UPSTREAM_PRODUCT_NAME,)
)


def _format_cli_version(version: str) -> str:
    """Return the branded version string printed by ``--version``.

    Parameters
    ----------
    version : str
        Installed distribution version.

    Returns
    -------
    str
        Product name followed by the supplied version.
    """
    return f"{PRIMARY_CLI_NAME} {version}"
