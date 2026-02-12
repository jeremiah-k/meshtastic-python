# BLE Integration Guide

This document summarizes the current BLE implementation, common pitfalls seen in the field, and recommended patterns for client code that embeds `meshtastic-python`.

## Architecture highlights

- **Single connection gate per address.** A process-wide registry prevents overlapping connects to the same normalized BLE address. When another interface instance already owns the address you will see `Connection suppressed: recently connected elsewhere`.
- **Short direct connect, then discovery.** When an address is known we try a short direct connect (~12s). If that fails we run a 10s scan and then perform a full connect using the configured `BLEConfig.CONNECTION_TIMEOUT` (default 60s).
- **Disconnect handling is serialized.** A per-interface lock drops stale duplicate disconnect callbacks and marks the address as disconnected before scheduling auto-reconnect.
- **Singleton BLE runner.** All BLE operations are managed by a singleton `BLECoroutineRunner` with a shared background thread and asyncio event loop. This reduces resource usage from N threads to 1 thread regardless of how many clients exist and simplifies cleanup.

## Recommended usage

### Keep one interface per device address

```python
from meshtastic.interfaces.ble import BLEInterface
from pubsub import pub

# Subscribe BEFORE constructing BLEInterface to ensure early packets aren't missed
# BLEInterface automatically attempts connection during construction
pub.subscribe(lambda packet, interface: print(packet), "meshtastic.receive")

# Create a single long-lived interface for the process
iface = BLEInterface(address="DD:DD:13:27:74:29", auto_reconnect=True)

# Connection is already established; auto-reconnect will take over after disconnects
```

Avoid creating new `BLEInterface` instances on every reconnect attempt. Reuse the same instance so the per-address gate and state manager can coordinate cleanly.

### Let auto-reconnect run alone

If you enable `auto_reconnect=True` (default), do not layer an application-level reconnect loop on top. Duplicate reconnect loops cause "suppressed duplicate connect" logs and can interfere with the built-in recovery.

If your application manages reconnects itself, disable the built-in loop:

```python
# Note: With auto_reconnect=False, initial connection still happens in __init__
iface = BLEInterface(address="AA:BB:CC:DD:EE:FF", auto_reconnect=False)
# on disconnect: call iface.connect() again using the same instance
```

**When to call `connect()` manually:** The BLEInterface constructor automatically calls `connect()`. You only need to call it manually after `close()` or when handling a disconnect with `auto_reconnect=False`.

### Respect the address gate

When you see `Connection suppressed: recently connected elsewhere`, another interface in this process holds the connection. Back off instead of retrying immediately; retries within a few seconds will continue to be suppressed.

### Keep retries bounded

- Direct connect is already short; the discovery fallback can block up to `BLEConfig.CONNECTION_TIMEOUT` (60s). After a failed scan + connect cycle, wait at least 30–60s before retrying to avoid overwhelming Linux/BlueZ.
- If you cannot enumerate connected devices on your backend (common on Linux), a scan returning zero devices is expected when the peripheral is already connected elsewhere. Rely on the address gate rather than repeated scans in that case.

### Log interpretation

- `Ignoring disconnect … while a connection is in progress.` — benign stale callback during CONNECTING.
- `Connection suppressed: recently connected elsewhere` — per-address gate blocked a duplicate attempt.
- `Cannot connect while interface is closing` — an earlier connect failed and the interface is still shutting down its BLE client; wait and retry with the same instance.

## Minimal pubsub example

```python
from meshtastic.interfaces.ble import BLEInterface
from pubsub import pub

def on_packet(packet, interface):
    print("Packet:", packet)

# Subscribe BEFORE constructing BLEInterface to ensure early packets aren't missed
pub.subscribe(on_packet, "meshtastic.receive")

# BLEInterface automatically connects during construction
iface = BLEInterface(address="DD:DD:13:27:74:29", auto_reconnect=True)

try:
    # Keep your application alive; work is driven by callbacks
    import time
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    iface.close()
```

## Common pitfalls and how to avoid them

- **Multiple interface instances per address:** reuse one instance; otherwise you will collide with the address gate and slow down recovery.
- **Layered reconnect loops:** pick either the library’s auto-reconnect or your own, not both.
- **Aggressive retry cadence:** back off after a failed scan + connect cycle; long blocking calls during teardown can exhaust resources on Linux/BlueZ.
- **Skipping notification re-registration:** use the same interface instance so built-in notification tracking can resubscribe after reconnects.

## When to restart the process

Restart if you observe any of:

- repeated `Cannot connect while interface is closing` for minutes,
- repeated scan+connect cycles timing out on Linux/BlueZ despite the device being powered and advertising.

A clean restart clears any leaked BLE/DBus handles and resets the address gate.
