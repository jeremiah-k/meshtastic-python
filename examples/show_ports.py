"""Simple program to show serial ports."""

from meshtastic.util import findPorts


def main() -> None:
    """Print discovered serial ports for local debugging."""
    print(findPorts())


if __name__ == "__main__":
    main()
