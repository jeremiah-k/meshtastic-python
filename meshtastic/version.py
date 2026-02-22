"""Version lookup utilities, isolated for cleanliness."""


def get_active_version() -> str:
    """
    Retrieve the active installed version of the "meshtastic" package.

    Returns:
        str: The package version string, or "unknown" if the distribution metadata cannot be found.
    """
    try:
        from importlib.metadata import (  # pylint: disable=import-outside-toplevel
            PackageNotFoundError,
            version,
        )

        return version("meshtastic")
    except PackageNotFoundError:
        return "unknown"
    except ImportError:
        # Fall back to pkg_resources for older Python versions
        import pkg_resources  # pylint: disable=import-outside-toplevel

        try:
            return pkg_resources.get_distribution("meshtastic").version
        except pkg_resources.DistributionNotFound:
            return "unknown"
