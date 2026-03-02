"""Version lookup utilities, isolated for cleanliness."""

from importlib.metadata import PackageNotFoundError, version


# Ordered candidates for installed distribution metadata resolution.
# Fork builds can publish under an alternate package name while keeping
# the import package as `meshtastic`.
DISTRIBUTION_NAME_CANDIDATES: tuple[str, ...] = ("mtjk", "meshtastic")


def get_active_version() -> str:
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
