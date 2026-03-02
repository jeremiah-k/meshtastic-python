"""Version lookup utilities, isolated for cleanliness."""

from importlib.metadata import PackageNotFoundError, version


def get_active_version() -> str:
    """Retrieve the active installed version of the "meshtastic" package.

    Returns
    -------
    str
        The package version string, or "unknown" if the distribution metadata cannot be found.
    """
    try:
        return version("meshtastic")
    except PackageNotFoundError:
        return "unknown"
