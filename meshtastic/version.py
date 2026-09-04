"""Version lookup utilities, isolated for cleanliness."""

from importlib.metadata import PackageNotFoundError, version

from meshtastic._branding import (
    DISTRIBUTION_NAME,
)
from meshtastic._branding import PROJECT_DISPLAY_NAME as _PROJECT_DISPLAY_NAME
from meshtastic._branding import UPSTREAM_PRODUCT_NAME

# COMPAT_STABLE_SHIM: historical constants retained for callers importing them.
PACKAGE_NAME: str = DISTRIBUTION_NAME
PROJECT_DISPLAY_NAME: str = _PROJECT_DISPLAY_NAME

# Ordered candidates for installed distribution metadata resolution.
# Fork builds can publish under an alternate package name while keeping
# the import package as `meshtastic`.
DISTRIBUTION_NAME_CANDIDATES: tuple[str, ...] = tuple(
    dict.fromkeys((PACKAGE_NAME, UPSTREAM_PRODUCT_NAME))
)

# Recommended one-liner for upgrading the package.
# Uses pipx, the recommended installer for the CLI distribution.
INSTALL_UPGRADE_HINT: str = f"pipx upgrade {PACKAGE_NAME}"


def getActiveVersion() -> str:
    """Retrieve the active installed package version.

    The lookup tries each candidate distribution name in
    ``DISTRIBUTION_NAME_CANDIDATES`` and returns the first installed version.

    Returns
    -------
    str
        The package version string, or "unknown" if the distribution metadata cannot be found.
    """
    for distribution_name in DISTRIBUTION_NAME_CANDIDATES:
        try:
            return version(distribution_name)
        except PackageNotFoundError:
            continue
    return "unknown"


# COMPAT_STABLE_SHIM: historical snake_case alias.
def get_active_version() -> str:
    """Compatibility alias for `getActiveVersion`.

    Returns
    -------
    str
        Active version string resolved by getActiveVersion().
    """
    return getActiveVersion()
