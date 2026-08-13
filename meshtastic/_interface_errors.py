"""Leaf exception definitions shared across interface-adjacent runtimes."""


class MeshInterfaceError(Exception):
    """General MeshInterface operation error with a human-readable message."""

    def __init__(self, message: str) -> None:
        """Create an interface error with its historical ``message`` attribute.

        Parameters
        ----------
        message : str
            Human-readable description of the interface failure.
        """
        self.message = message
        super().__init__(self.message)


# Preserve the historical nested-class metadata as well as identity through the
# MeshInterface compatibility alias. This keeps repr/pickle/introspection stable
# while allowing internal modules to depend on a leaf exception module.
MeshInterfaceError.__module__ = "meshtastic.mesh_interface"
MeshInterfaceError.__qualname__ = "MeshInterface.MeshInterfaceError"
