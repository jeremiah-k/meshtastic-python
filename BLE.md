# BLE Integration Guide

This document covers the current BLE implementation, common field pitfalls, and
recommended patterns for code that embeds `meshtastic-python`.

---

## Architecture overview

| Component                                | Responsibility                                                                                |
| ---------------------------------------- | --------------------------------------------------------------------------------------------- |
| `BLEInterface`                           | User-facing entry point; extends `MeshInterface` with BLE lifecycle management                |
| `BLEClient`                              | Synchronous wrapper around Bleak; delegates async calls to the singleton runner               |
| `BLECoroutineRunner`                     | Process-wide singleton with one background thread and one asyncio event loop                  |
| `BLEStateManager`                        | Centralized state machine (`DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTING`)           |
| `BLEErrorHandler`                        | Unified exception-handling helpers used across the BLE subsystem                              |
| `NotificationManager`                    | Tracks active GATT notification subscriptions so they can be resubscribed after reconnects    |
| `DiscoveryManager`                       | Scan-based BLE device discovery with address normalization                                    |
| `ConnectionValidator`                    | Enforces connection preconditions before any lock is acquired                                 |
| `ClientManager`                          | Owns `BLEClient` lifecycle and safe-close operations                                          |
| `ConnectionOrchestrator`                 | Coordinates a full connection attempt: validate → discover → connect → register notifications |
| `ReconnectScheduler` / `ReconnectWorker` | Policy-driven background reconnect loop                                                       |

### Key design choices

- **Process-wide address gate.** A registry in
  [`meshtastic/interfaces/ble/gating.py`](meshtastic/interfaces/ble/gating.py)
  prevents two live interfaces from connecting to the same normalized BLE
  address at the same time. Stale (dead-owner) claims are pruned automatically
  after `BLEConfig.CONNECTION_GATE_UNOWNED_STALE_SECONDS` (default 300 s).
  A suppressed attempt logs `Connection suppressed: recently connected elsewhere`.

- **Short direct connect, then scan fallback.** A direct connect is tried first
  (see `DIRECT_CONNECT_TIMEOUT_SECONDS` in
  [`meshtastic/interfaces/ble/connection.py`](meshtastic/interfaces/ble/connection.py),
  default 12 s). On failure, a 10 s scan runs and then a full connect uses
  `BLEConfig.CONNECTION_TIMEOUT` (default 60 s).

- **Serialized disconnect handling.** A per-interface `_disconnect_lock`
  deduplicates concurrent disconnect callbacks and marks the address as free
  before scheduling any optional auto-reconnect.

- **Singleton BLE event loop.** All Bleak I/O goes through one shared
  `BLECoroutineRunner`: N clients share 1 background thread, which lowers
  resource usage and simplifies teardown.

---

## Locking rules

When code paths must hold multiple BLE locks, always acquire in this order to
prevent deadlocks:

1. Global registry lock (`_REGISTRY_LOCK` in
   [`meshtastic/interfaces/ble/gating.py`](meshtastic/interfaces/ble/gating.py))
2. Per-address lock (`_addr_lock_context` in the same module)
3. Interface connect lock (`_connect_lock`)
4. Interface state lock (`_state_lock`)
5. Interface disconnect lock (`_disconnect_lock`)

**Exception:** `_handle_disconnect()` acquires `_disconnect_lock` _first_ in
non-blocking mode. If another disconnect handler is already active, the method
returns immediately rather than waiting, which prevents lock inversion while
still deduplicating callbacks.

---

## Internal helper APIs (for library contributors)

These classes are part of the `meshtastic.interfaces.ble` package and are
**not** part of the stable public surface exposed through
`meshtastic.ble_interface`. They are documented here for contributors who
extend or test the BLE subsystem.

### `BLEErrorHandler`

All exception-handling patterns in the BLE subsystem go through this class.

```python
from meshtastic.interfaces.ble.errors import BLEErrorHandler

handler = BLEErrorHandler()

# Execute a callable; return a fallback on any handled exception.
result = handler._safe_execute(
    lambda: risky_call(),
    default_return=None,
    error_msg="risky_call failed",
    reraise=False,
)

# Best-effort cleanup: suppresses all non-exit exceptions, returns bool.
ok = handler._safe_cleanup(lambda: resource.close(), "resource close")
```

`_safe_execute` swallows `BleakError`, `DecodeError`, and
`concurrent.futures.TimeoutError`; other exceptions are also caught unless
`reraise=True`. `SystemExit` and `KeyboardInterrupt` always propagate.

### `NotificationManager`

Tracks GATT notification subscriptions so they can be resubscribed cleanly
after a reconnect.

```python
from meshtastic.interfaces.ble.notifications import NotificationManager

mgr = NotificationManager()

# Register a callback for a characteristic UUID; returns an opaque token.
token = mgr._subscribe(uuid, callback)

# Retrieve the most-recently-registered callback for a UUID.
cb = mgr._get_callback(uuid)

# Stop all notifications through a BLEClient (e.g. during shutdown).
mgr._unsubscribe_all(client, timeout=5.0)

# Re-register all subscriptions on a new client (e.g. after reconnect).
mgr._resubscribe_all(client, timeout=5.0)

# Clear internal subscription state (called after full disconnect + cleanup).
mgr._cleanup_all()
```

### `RetryPolicy` / `ReconnectPolicy`

The BLE read loop and auto-reconnect use policies from
[`meshtastic/interfaces/ble/policies.py`](meshtastic/interfaces/ble/policies.py).

`ReconnectPolicy` is an internal BLE policy utility and uses
underscore-prefixed snake_case helpers in the BLE subsystem:

```python
from meshtastic.interfaces.ble.policies import RetryPolicy

policy = RetryPolicy.emptyRead()  # or .transientError() / .autoReconnect()

delay = policy._get_delay(attempt)       # float, jittered exponential backoff
should_go = policy._should_retry(count)  # bool, respects max_retries
delay, ok = policy.next_attempt()        # combined: compute delay + advance counter
```

For compatibility with existing Python projects, the stable BLE surface exposed
through `meshtastic.ble_interface` keeps the legacy snake_case method names
from the pre-refactor API (for example `find_device`, `read_gatt_char`,
`start_notify`).

Contributor rule for naming updates:

1. Keep legacy snake_case BLE public methods callable.
2. Keep only the approved BLE camelCase promotions callable:
   `findDevice`, `isConnected`, and `stopNotify`.
3. Route compatibility names to one implementation (prefer internal
   underscore-prefixed helpers).
4. Do not add new BLE aliases unless there is an explicit compatibility need.
5. Do not remove compatibility wrappers unless there is an explicit breaking
   change decision.

---

## Recommended usage

### One interface per device address

```python
from meshtastic.interfaces.ble import BLEInterface
from pubsub import pub
import time

# Subscribe BEFORE constructing BLEInterface to avoid missing early packets.
pub.subscribe(lambda packet, interface: print(packet), "meshtastic.receive")

# BLEInterface connects automatically during construction.
iface = BLEInterface(address="DD:DD:13:27:74:29")
```

Reuse the same `BLEInterface` instance for the lifetime of the connection to
the device. Creating new instances repeatedly collides with the address gate
and slows recovery.

### Auto-reconnect is opt-in

```python
# Built-in reconnect loop (recommended for long-running integrations):
iface = BLEInterface(address="AA:BB:CC:DD:EE:FF", auto_reconnect=True)

# Manual reconnect (your code drives reconnects instead):
iface = BLEInterface(address="AA:BB:CC:DD:EE:FF", auto_reconnect=False)
# On disconnect: call iface.connect() on the same instance.
```

Do not layer both simultaneously — duplicate reconnect loops produce
`suppressed duplicate connect` log entries and can interfere with the built-in
recovery logic.

CLI opt-in:

```bash
meshtastic --ble any --ble-auto-reconnect --listen
```

**When to call `connect()` manually:** the constructor calls it for you. Only
call it again after `close()` or when you are managing reconnects yourself with
`auto_reconnect=False`.

### Respect the address gate

When you see `Connection suppressed: recently connected elsewhere`, another
interface in this process owns the connection. Back off; retries within a few
seconds will continue to be suppressed.

### Keep retries bounded

- The scan + connect cycle can block up to `BLEConfig.CONNECTION_TIMEOUT` (60 s).
  After a failed cycle, wait at least 30–60 s before retrying to avoid
  exhausting Linux/BlueZ resources.
- A scan returning zero devices on Linux is expected when the peripheral is
  already connected elsewhere. Rely on the address gate; repeated scans make
  things worse.

---

## Minimal pubsub example

```python
from meshtastic.interfaces.ble import BLEInterface
from pubsub import pub
import time

def on_packet(packet, interface):
    print("Packet:", packet)

# Subscribe BEFORE constructing BLEInterface to ensure early packets aren't missed.
pub.subscribe(on_packet, "meshtastic.receive")

# Connection is established in the constructor.
iface = BLEInterface(address="DD:DD:13:27:74:29")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    iface.close()
```

---

## Context-manager pattern

`BLEInterface` supports the context-manager protocol and is the recommended way
to ensure `close()` is always called:

```python
with BLEInterface(address="DD:DD:13:27:74:29") as iface:
    iface.sendText("hello")
# close() is called automatically on any exit, including exceptions.
```

---

## Log interpretation quick reference

| Message                                                    | Meaning                                                                                                               |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `Ignoring disconnect … while a connection is in progress.` | Benign stale callback during CONNECTING; discard.                                                                     |
| `Connection suppressed: recently connected elsewhere`      | Per-address gate blocked duplicate attempt; back off.                                                                 |
| `Cannot connect while interface is closing`                | Interface is mid-shutdown; wait and retry with the same instance.                                                     |
| `Throttling BLE receive recovery: waiting Xs before retry` | Receive thread crashed repeatedly; exponential backoff active.                                                        |
| `BLE receive thread did not exit within Xs`                | Thread took longer than `RECEIVE_THREAD_JOIN_TIMEOUT` (2 s) to exit; non-fatal, but worth investigating for hung I/O. |

---

## Common pitfalls

| Pitfall                                       | Fix                                                                                                                               |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Multiple `BLEInterface` instances per address | Reuse one instance; multiple instances collide on the address gate.                                                               |
| Layered reconnect loops                       | Use either the library's `auto_reconnect=True` **or** your own loop, never both.                                                  |
| Aggressive retry cadence                      | Include exponential backoff; long `scan + connect` calls during rapid retries exhaust BlueZ.                                      |
| Forgetting to resubscribe notifications       | Use the same instance so `NotificationManager` can call `_resubscribe_all()` automatically after reconnects.                      |
| Not closing the interface                     | Always call `close()` or use the context-manager pattern; unclosed BLE handles on Linux prevent future connections (BlueZ quirk). |

---

## When to restart the process

Restart if you observe:

- repeated `Cannot connect while interface is closing` for several minutes, or
- repeated scan + connect cycles timing out on Linux/BlueZ despite the device
  being powered and advertising.

A clean restart clears leaked BLE/DBus handles and resets the process-wide
address gate.
