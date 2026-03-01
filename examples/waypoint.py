"""Create or delete a waypoint.

To run:
python3 examples/waypoint.py --port /dev/ttyUSB0 create 45 test desc 128205 '2024-12-18T23:05:23' 48.74 7.35
python3 examples/waypoint.py delete 45
"""

import argparse
import datetime
import sys

import meshtastic.serial_interface

INVALID_EXPIRE_MSG = 'Invalid "expire": {!r}; expected ISO8601 format.'


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for waypoint operations."""
    parser = argparse.ArgumentParser(
        prog="waypoint", description="Create and delete Meshtastic waypoints"
    )
    parser.add_argument(
        "--port",
        default=None,
        help="Serial port of the Meshtastic device (auto-detected if not specified)",
    )
    parser.add_argument(
        "--debug",
        default=False,
        action="store_true",
        help="Enable debug output to stderr",
    )

    subparsers = parser.add_subparsers(dest="cmd", required=True)

    parser_delete = subparsers.add_parser("delete", help="Delete a waypoint")
    parser_delete.add_argument("id", type=int, help="ID of the waypoint")

    parser_create = subparsers.add_parser("create", help="Create a new waypoint")
    parser_create.add_argument("id", type=int, help="ID of the waypoint")
    parser_create.add_argument("name", help="Name of the waypoint")
    parser_create.add_argument("description", help="Description of the waypoint")
    parser_create.add_argument("icon", type=int, help="Icon of the waypoint")
    parser_create.add_argument(
        "expire",
        help=(
            "Expiration date interpreted by datetime.fromisoformat "
            "(for example 2024-12-18T23:05:23); interpreted as UTC if no timezone specified"
        ),
    )
    parser_create.add_argument("latitude", type=float, help="Latitude")
    parser_create.add_argument("longitude", type=float, help="Longitude")
    return parser


def main() -> None:
    """Execute waypoint create/delete command against a serial-connected radio."""
    args = _build_parser().parse_args()

    expire_unix: int | None = None
    if args.cmd == "create":
        try:
            parsed_expire = datetime.datetime.fromisoformat(args.expire)
            if parsed_expire.tzinfo is None:
                parsed_expire = parsed_expire.replace(tzinfo=datetime.timezone.utc)
            expire_unix = int(parsed_expire.timestamp())
        except ValueError:
            raise SystemExit(INVALID_EXPIRE_MSG.format(args.expire)) from None

    # By default this will auto-detect a Meshtastic device.
    debug_out = sys.stderr if args.debug else None
    with meshtastic.serial_interface.SerialInterface(
        args.port, debugOut=debug_out
    ) as iface:
        if args.cmd == "create":
            result = iface.sendWaypoint(
                waypoint_id=args.id,
                name=args.name,
                description=args.description,
                icon=args.icon,
                expire=expire_unix,
                latitude=args.latitude,
                longitude=args.longitude,
            )
        else:
            result = iface.deleteWaypoint(args.id)
        print(result)


if __name__ == "__main__":
    main()
