"""Program to scan for hardware.

To run: `python examples/scan_for_devices.py`.
"""

import sys

from meshtastic.util import (
    active_ports_on_supported_devices,
    detect_supported_devices,
    get_unique_vendor_ids,
)


def main() -> None:
    """Print detected supported Meshtastic devices and active ports."""
    if len(sys.argv) != 1:
        print(f"usage: {sys.argv[0]}")
        print("Detect which device we might have.")
        raise SystemExit(3)

    vendor_ids = sorted(get_unique_vendor_ids())
    print(f"Searching for all devices with these vendor ids {vendor_ids}")

    supported_devices = detect_supported_devices()
    if supported_devices:
        print("Detected possible devices:")
        sorted_devices = sorted(
            supported_devices,
            key=lambda d: (d.name, d.version or "", d.for_firmware or ""),
        )
        for device in sorted_devices:
            version_suffix = f" {device.version}" if device.version else ""
            firmware_info = device.for_firmware or "unknown"
            print(f" name:{device.name}{version_suffix} firmware:{firmware_info}")
    else:
        print("No supported devices detected.")

    ports = active_ports_on_supported_devices(supported_devices)
    print(f"ports:{sorted(ports)}")


if __name__ == "__main__":
    main()
